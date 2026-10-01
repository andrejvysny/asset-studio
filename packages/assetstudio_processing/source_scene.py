"""Static content rules for source-package members: text resources, shaders, closure, capabilities.

Grammar: contracts/godot-integration/v1/static-source-package.md §3-§5. Nothing is loaded or executed.
"""
from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from assetstudio_core.source_manifest import AssetDependencyEntry, PackageFileEntry, SourcePackageManifestV1

from .godot_text import (
    MAX_INPUT_BYTES,
    Call,
    GodotTextDoc,
    GodotTextError,
    Ref,
    Section,
    iter_refs,
    parse_godot_text,
    section_refs,
    walk,
)
from .source_media import IMAGE_KINDS, check_glb, check_image
from .source_report import ProblemSink, SourcePackageError
from .source_structure import NodeDef, SceneStructure, build_structure, find_cycles, scene_defs

SCRIPT_TYPES = frozenset({"Script", "GDScript", "CSharpScript", "GDExtension"})
SECTION_KINDS = frozenset({"ext_resource", "sub_resource", "node", "resource", "editable"})
SHADER_SUFFIXES = (".gdshader", ".gdshaderinc")
CONSTRUCTORS = frozenset({
    "Vector2", "Vector2i", "Vector3", "Vector3i", "Vector4", "Vector4i", "Color", "Transform2D", "Transform3D",
    "Basis", "Quaternion", "AABB", "Plane", "Rect2", "Rect2i", "Projection", "StringName", "NodePath", "Array",
    "Dictionary", "PackedByteArray", "PackedInt32Array", "PackedInt64Array", "PackedFloat32Array",
    "PackedFloat64Array", "PackedStringArray", "PackedVector2Array", "PackedVector3Array", "PackedVector4Array",
    "PackedColorArray"})
_INCLUDE_LINE = re.compile(r"^[ \t]*#[ \t]*include\b(.*)$", re.M)
_INCLUDE_ARG = re.compile(r'^[ \t]*"([^"\n]*)"[ \t]*(?://.*)?$')


@dataclass
class Facts:
    reached: list[str] = field(default_factory=list)
    deps: list[str] = field(default_factory=list)
    detected: list[str] = field(default_factory=list)
    structure: SceneStructure | None = None


class PackageChecker:
    def __init__(self, manifest: SourcePackageManifestV1, staging: Path, policy: Mapping[str, Any],
                 sink: ProblemSink) -> None:
        self.manifest, self.staging, self.sink = manifest, staging, sink
        self.files = {f.path for f in manifest.files}
        self.allowed_nodes = frozenset(policy["allowed_node_types"])
        self.allowed_resources = frozenset(policy["allowed_resource_types"])
        self.edges: dict[str, set[str]] = {}
        self.deps: dict[str, set[str]] = {}
        self.instances: dict[str, set[str]] = {}  # .tscn -> package .tscn it instances
        self.includes: dict[str, set[str]] = {}  # shader-bearing file -> shader file it #includes
        self.scenes: dict[str, list[NodeDef]] = {}
        self.detected = {"godot_text_scene_v1"}

    def run(self) -> Facts:
        for path in sorted(self.files):
            try:
                self._check_file(path)
            except SourcePackageError as e:
                self.sink.add(e.problem)
        reached, deps = self._closure()
        self._capabilities()
        structure = self._structure()
        order = ("godot_text_scene_v1", "csg_static", "static_collision", "shader_source")
        return Facts(sorted(reached), sorted(deps), [c for c in order if c in self.detected], structure)

    def _structure(self) -> SceneStructure | None:
        cyclic = False
        for kind, graph in (("instance_cycle", self.instances), ("include_cycle", self.includes)):
            for cycle in find_cycles(graph):
                cyclic = True
                self.sink.error("unsafe_package", kind, "cycle: " + " -> ".join(cycle), cycle[0])
        if cyclic:
            return None
        try:
            return build_structure(self.scenes, self.manifest.entry_scene)
        except SourcePackageError as e:
            self.sink.add(e.problem)
            return None

    def _check_file(self, path: str) -> None:
        suffix = posixpath.splitext(path)[1]
        file = self.staging / path
        if suffix in (".tscn", ".tres"):
            self._text(path, file, suffix)
        elif suffix in SHADER_SUFFIXES:
            self._shader(path, file)
        elif suffix in IMAGE_KINDS:
            check_image(path, file, suffix)
        elif suffix == ".glb":
            check_glb(path, file)

    def _read_text(self, path: str, file: Path) -> bytes:
        if file.stat().st_size > MAX_INPUT_BYTES:
            raise SourcePackageError("resource_limit", "input_limit", "text member larger than the parser limit", path)
        return file.read_bytes()

    def _text(self, path: str, file: Path, suffix: str) -> None:
        try:
            doc = parse_godot_text(self._read_text(path, file))
        except GodotTextError as e:
            raise SourcePackageError(e.code, e.detail, str(e), path) from e
        expected = "gd_scene" if suffix == ".tscn" else "gd_resource"
        if doc.kind != expected:
            raise SourcePackageError("unsafe_package", "wrong_header", f"{suffix} must start with [{expected}]", path)
        rules = _TextRules(self, path, doc)
        rules.run()
        self.edges.setdefault(path, set()).update(rules.paths)
        self.deps.setdefault(path, set()).update(rules.deps)

    def _shader(self, path: str, file: Path) -> None:
        try:
            text = self._read_text(path, file).decode("utf-8")
        except UnicodeDecodeError as e:
            raise SourcePackageError("unsafe_package", "not_utf8", "shader is not valid UTF-8", path) from e
        self.detected.add("shader_source")
        self.check_includes(path, text, 0)

    def check_includes(self, path: str, text: str, base_line: int) -> None:
        for m in _INCLUDE_LINE.finditer(text):
            arg = _INCLUDE_ARG.match(m.group(1))
            target = self.resolve_include(path, arg.group(1)) if arg else None
            if target is None:
                line = base_line + text.count("\n", 0, m.start()) + 1
                self.sink.error("unsafe_package", "shader_include_escape",
                                f"#include {m.group(1).strip()} leaves the package or is unmapped (line {line})", path)
            else:
                self.edges.setdefault(path, set()).add(target)
                self.includes.setdefault(path, set()).add(target)

    def resolve_include(self, from_path: str, target: str) -> str | None:
        if target.startswith("res://"):
            entry = self.manifest.resource_map.get(target)
            return entry.path if isinstance(entry, PackageFileEntry) else None
        if "://" in target or "\\" in target or target.startswith("/") or "\x00" in target:
            return None
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(from_path), target))
        return joined if joined in self.files and joined.endswith(SHADER_SUFFIXES) else None

    def _closure(self) -> tuple[set[str], set[str]]:
        reached: set[str] = set()
        queue = [self.manifest.entry_scene]
        while queue:
            path = queue.pop()
            if path not in reached:
                reached.add(path)
                queue.extend(self.edges.get(path, ()))
        for path in sorted(self.files - reached):
            self.sink.error("unsafe_package", "unreferenced_file", "declared file is not reachable from entry_scene",
                            path)
        return reached, {d for p in reached for d in self.deps.get(p, ())}

    def _capabilities(self) -> None:
        declared = set(self.manifest.capabilities)
        for cap in ("csg_static", "static_collision", "shader_source"):
            if cap in self.detected and cap not in declared:
                self.sink.error("unsafe_package", "capability_undeclared",
                                f"package uses {cap} but the manifest does not declare it")


class _TextRules:
    """Rules for one parsed .tscn/.tres; collects problems on the sink, records references."""

    def __init__(self, ck: PackageChecker, path: str, doc: GodotTextDoc) -> None:
        self.ck, self.path, self.doc = ck, path, doc
        self.ext: dict[str, Section] = {}
        self.sub_ids: set[str] = set()
        self.target: dict[str, str | None] = {}  # ext id -> package file path | "dep:<key>"
        self.paths: set[str] = set()
        self.deps: set[str] = set()

    def err(self, detail: str, message: str, line: int | None = None, code: str = "unsafe_package") -> None:
        self.ck.sink.error(code, detail, f"{message} (line {line})" if line else message, self.path)

    def run(self) -> None:
        self._index()
        self._section_kinds()
        self._scripts()
        self._types()
        self._refs()
        self._constructors()
        self._ext_paths()
        self._nodes()
        self._inline_shaders()
        if self.path.endswith(".tscn"):
            self.ck.scenes[self.path] = scene_defs(self.doc, self.target)

    def _index(self) -> None:
        for sec in self.doc.ext_resources() + self.doc.sub_resources():
            rid = sec.attrs.get("id")
            seen = self.ext if sec.kind == "ext_resource" else self.sub_ids
            if not isinstance(rid, str) or rid in seen:
                self.err("malformed_section", f"{sec.kind} needs a unique string id", sec.line)
            elif sec.kind == "ext_resource":
                self.ext[rid] = sec
            else:
                self.sub_ids.add(rid)

    def _section_kinds(self) -> None:
        for sec in self.doc.sections:
            if sec.kind == "connection":
                self.err("connection", "signal connections are not allowed", sec.line)
            elif sec.kind not in SECTION_KINDS:
                self.err("section_not_allowed", f"section [{sec.kind}] is not allowed", sec.line)

    def _scripts(self) -> None:
        if "script_class" in self.doc.header:
            self.err("script_property", "header declares script_class", 1)
        for sec in self.doc.sections:
            if "script_class" in sec.attrs:
                self.err("script_property", "section declares script_class", sec.line)
            for key, _ in sec.props:
                if key == "script" or key.startswith("metadata/_custom_type_script"):
                    self.err("script_property", f"property {key!r} attaches a script", sec.line)
            self._script_refs(sec)

    def _script_refs(self, sec: Section) -> None:
        for ref in section_refs(sec):
            target = self.ext.get(ref.id) if ref.kind == "ExtResource" else None
            if target is not None and target.attrs.get("type") in SCRIPT_TYPES:
                self.err("script_property", "value references a Script resource", sec.line)
                return

    def _types(self) -> None:
        allowed = self.ck.allowed_resources
        for sec in self.doc.ext_resources() + self.doc.sub_resources():
            self._type_in(sec.attrs.get("type"), allowed, f"{sec.kind} type", sec.line)
        if self.doc.kind == "gd_resource":
            self._type_in(self.doc.header.get("type"), allowed, "gd_resource type", 1)
        for sec in self.doc.nodes():
            node_type = sec.attrs.get("type")
            if node_type is None and "instance" in sec.attrs:
                continue
            if node_type is None:
                self.err("node_missing_type", "node has neither type nor instance", sec.line)
            else:
                self._type_in(node_type, self.ck.allowed_nodes, "node type", sec.line)

    def _type_in(self, value: Any, allowed: frozenset[str], what: str, line: int) -> None:
        if not isinstance(value, str) or value in SCRIPT_TYPES or value not in allowed:
            self.err("type_not_allowed", f"{what} {value!r} is not in the allowlist", line)

    def _refs(self) -> None:
        for sec in self.doc.sections:
            for ref in section_refs(sec):
                known = self.ext if ref.kind == "ExtResource" else self.sub_ids
                if ref.id not in known:
                    self.err("unresolved_ref", f"{ref.kind}({ref.id!r}) is not defined in this file", sec.line)
        for ref in (r for v in self.doc.header.values() for r in iter_refs(v)):
            self.err("unresolved_ref", f"header references {ref.kind}({ref.id!r})", 1)

    def _constructors(self) -> None:
        for sec in self.doc.sections:
            values = [*sec.attrs.values(), *(v for _, v in sec.props)]
            for node in (n for v in values for n in walk(v)):
                if isinstance(node, Call) and node.name.split("[", 1)[0] not in CONSTRUCTORS:
                    self.err("constructor_not_allowed", f"constructor {node.name}() is not allowed", sec.line)

    def _ext_paths(self) -> None:
        manifest = self.ck.manifest
        for rid, sec in self.ext.items():
            path = sec.attrs.get("path")
            entry = manifest.resource_map.get(path) if isinstance(path, str) else None
            if entry is None:
                self.err("unmapped_reference", f"ext_resource path {path!r} is not in resource_map", sec.line)
                continue
            if isinstance(entry, PackageFileEntry):
                self.paths.add(entry.path)
                self.target[rid] = entry.path
                scene_like = entry.path.endswith((".glb", ".tscn"))
            else:
                self._dependency(entry, sec)
                self.target[rid] = f"dep:{entry.asset_key}"
                scene_like = True
            if scene_like and sec.attrs.get("type") != "PackedScene":
                self.err("bad_reference_type", f"{path!r} must be an ext_resource of type PackedScene", sec.line)

    def _dependency(self, entry: AssetDependencyEntry, sec: Section) -> None:
        if entry.asset_key not in self.ck.manifest.asset_dependencies:
            self.err("unknown_asset_key", "asset_key is not in asset_dependencies", sec.line,
                     "unsupported_source_dependency")
        self.deps.add(entry.asset_key)

    def _nodes(self) -> None:
        for sec in self.doc.nodes():
            node_type = sec.attrs.get("type")
            if isinstance(node_type, str) and node_type.startswith("CSG"):
                self.ck.detected.add("csg_static")
            if node_type == "CollisionShape3D":
                self.ck.detected.add("static_collision")
            if "instance" in sec.attrs:
                self._instance(sec)

    def _instance(self, sec: Section) -> None:
        ref = sec.attrs["instance"]
        target = self.target.get(ref.id) if isinstance(ref, Ref) and ref.kind == "ExtResource" else None
        if target is None or not (target.startswith("dep:") or target.endswith((".tscn", ".glb"))):
            self.err("bad_instance", "instance must reference a package .tscn/.glb or an asset dependency", sec.line)
        elif target.endswith(".tscn"):
            self.ck.instances.setdefault(self.path, set()).add(target)

    def _inline_shaders(self) -> None:
        for sec in self.doc.sections:
            kind = sec.attrs.get("type") if sec.kind == "sub_resource" else self.doc.header.get("type")
            if sec.kind not in ("sub_resource", "resource") or kind != "Shader":
                continue
            for key, value in sec.props:
                if key == "code" and isinstance(value, str):
                    self.ck.detected.add("shader_source")
                    self.ck.check_includes(self.path, value, sec.line)
