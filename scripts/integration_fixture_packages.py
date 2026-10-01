"""Source-package fixtures: valid GodotStaticSourcePackageV1 zips and hostile archives (outcomes: INDEX.json)."""
from __future__ import annotations

import hashlib
from typing import Any

import integration_fixture_sources as src
from assetstudio_core.canonical_v1 import asset_key, canonical_bytes
from assetstudio_core.delivery import AssetDescriptorV1
from assetstudio_core.source_manifest import SourcePackageManifestV1, source_manifest_bytes
from integration_fixture_sources import LIB_A, LIB_B, MEDIA, SERVER, Member

Json = dict[str, Any]
Descs = dict[str, tuple[AssetDescriptorV1, bytes]]
PROP, PROP2, PROP_B, TREE, ROCK, HUT, CRYSTAL, CLUSTER = (
    ("prop", "aa", "v1"), ("prop2", "aa", "v2"), ("propb", "aa", "v1"), ("tree", "ab", "v1"), ("rock", "ac", "v1"),
    ("hut", "ad", "v1"), ("crystal", "ae", "v1"), ("cluster", "af", "v1"))
EXACT: Json = {"portable_status": "exact", "omissions": [], "approximations": []}
CRYSTAL_REPORT: Json = {
    "portable_status": "approximated", "omissions": [],
    "approximations": [{"slot_id": "crystal",
                        "reason": "custom shader replaced by a tinted StandardMaterial3D in the portable GLB"}]}
BASE = ["godot_text_scene_v1"]
PROP_DELIVERY = "dlv_00000000000000d1"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ref(spec: tuple[str, str, str]) -> Json:
    name, asset, ver = spec
    lib = LIB_B if name == "propb" else LIB_A
    return {"server_id": SERVER, "library_id": lib, "asset_id": f"ast_00000000000000{asset}",
            "version_id": f"ver_00000000000000{ver}"}


def key_of(r: Json) -> str:
    return asset_key(r["server_id"], r["library_id"], r["asset_id"], r["version_id"])


def _media(path: str) -> str:
    return MEDIA.get(path[path.rindex("."):], "application/octet-stream")


def build_package_doc(files: dict[str, bytes], entry: str, desc: AssetDescriptorV1, caps: list[str], report: Json,
                      deps: dict[str, Json] | None = None, dep_paths: dict[str, tuple[str, str]] | None = None,
                      undeclared: frozenset[str] = frozenset()) -> Json:
    """source_manifest.json content derived from the files; resource_map covers every res:// reference found."""
    dep_paths = dep_paths or {}
    rmap: Json = {}
    for res, uid in src.scan_references(files).items():
        rel = res.removeprefix("res://")
        if res in dep_paths:
            rmap[res] = {"kind": "asset_dependency", "asset_key": dep_paths[res][0], "entrypoint": dep_paths[res][1],
                         "original_uid": uid}
        elif rel in files:
            rmap[res] = {"kind": "package_file", "path": rel, "original_uid": uid}
    d = desc.model_dump(mode="json")
    placement = {k: d[k] for k in ("placement_anchor", "footprint_radius_m", "scale_range", "height_offset_range_m",
                                   "default_grounding")}
    placement["material_slots"] = [
        {"slot_id": s["slot_id"], "role": s["role"], "source_surfaces": s["surfaces"]["godot_static_source_v1"]}
        for s in d["material_slots"]]
    listed = [{"path": p, "sha256": sha(b), "size": len(b), "media_type": _media(p)}
              for p, b in sorted(files.items()) if p not in undeclared]
    return {"schema_version": 1, "entry_scene": entry, "source_godot_version": "4.4.1-stable", "files": listed,
            "resource_map": rmap, "asset_dependencies": deps or {}, "capabilities": caps,
            "conversion_report": report, "placement": placement}


def zip_of(manifest: bytes, files: dict[str, bytes], modes: dict[str, int] | None = None,
           encrypted: frozenset[str] = frozenset(), deflated: frozenset[str] = frozenset()) -> bytes:
    members = [Member("source_manifest.json", manifest)]
    members += [Member(n, b, (modes or {}).get(n, 0o644), n in encrypted, n in deflated) for n, b in files.items()]
    return src.build_zip(members)


def valid_packages(d: Descs) -> dict[str, tuple[bytes, SourcePackageManifestV1]]:
    prop_key = key_of(ref(PROP))
    cluster_deps = {prop_key: {"asset_ref": ref(PROP), "descriptor_sha256": sha(d["primitive_prop"][1]),
                               "representation": "portable_glb_v1", "delivery_id": PROP_DELIVERY}}
    plain = ({}, {})
    specs = {
        "primitive_prop": (src.prop_files(), "scenes/prop.tscn", BASE, EXACT, *plain),
        "textured_tree": (src.tree_files(), "scenes/tree.tscn", [*BASE, "pbr_textures", "alpha_mask"], EXACT, *plain),
        "vertex_color_rock_with_collision": (
            src.rock_files(), "scenes/rock.tscn", [*BASE, "static_collision", "vertex_colors"], EXACT, *plain),
        "csg_hut": (src.hut_files(), "scenes/hut.tscn", [*BASE, "csg_static"], EXACT, *plain),
        "custom_shader_crystal": (src.crystal_files(), "scenes/crystal.tscn", [*BASE, "shader_source"],
                                  CRYSTAL_REPORT, *plain),
        "primitive_prop_v2": (src.prop_v2_files(), "scenes/prop.tscn", BASE, EXACT, *plain),
        "prop_cluster": (src.cluster_files(), "scenes/cluster.tscn", BASE, EXACT, cluster_deps,
                         {"res://deps/primitive_prop.glb": (prop_key, "portable.glb")}),
    }
    out = {}
    for name, (files, entry, caps, report, deps, dep_paths) in specs.items():
        doc = build_package_doc(files, entry, d[name][0], caps, report, deps, dep_paths)
        model = SourcePackageManifestV1.model_validate(doc)
        out[name] = (zip_of(source_manifest_bytes(model), files), model)
    return out


# ---------------------------------------------------------------- hostile archives

Case = tuple[str, bytes, str, str, str]  # name, zip bytes, expected code, detail, description


def _make(desc: AssetDescriptorV1, files: dict[str, bytes], *, modes: dict[str, int] | None = None,
          undeclared: frozenset[str] = frozenset(), encrypted: frozenset[str] = frozenset(),
          deps: dict[str, Json] | None = None, dep_paths: dict[str, tuple[str, str]] | None = None,
          tamper_hash: bool = False, deflated: frozenset[str] = frozenset()) -> bytes:
    """Zip whose manifest is derived from `files` as written (never model-validated: it may be hostile too)."""
    doc = build_package_doc(files, "scenes/prop.tscn", desc, BASE, EXACT, deps, dep_paths, undeclared)
    if tamper_hash:
        doc["files"][0]["sha256"] = "0" * 64
    return zip_of(canonical_bytes(doc), files, modes, encrypted, deflated)


def _path_cases(desc: AssetDescriptorV1, prop: dict[str, bytes]) -> list[Case]:
    empty = b"[gd_scene format=3]\n"

    def make(extra: dict[str, bytes], **kw: Any) -> bytes:
        return _make(desc, {**prop, **extra}, **kw)

    return [
        ("path_traversal", make({"../x.tscn": empty}), "unsafe_package", "path_traversal",
         "member path '../x.tscn' escapes the package root"),
        ("absolute_path", make({"/etc/x.tscn": empty}), "unsafe_package", "absolute_path",
         "member path '/etc/x.tscn' is absolute"),
        ("backslash_path", make({"scenes\\evil.tscn": empty}), "unsafe_package", "backslash_path",
         "member name contains a backslash"),
        ("casefold_duplicate", make({"scenes/A.tscn": empty, "scenes/a.tscn": empty}), "unsafe_package",
         "casefold_collision", "scenes/A.tscn and scenes/a.tscn collide on case-insensitive filesystems"),
        ("symlink_member", make({"scenes/link.tscn": b"/etc/passwd"}, modes={"scenes/link.tscn": 0o120777}),
         "unsafe_package", "symlink", "member has S_IFLNK in external_attr"),
        ("encrypted_flag", make({}, encrypted=frozenset({"materials/prop_mat.tres"})), "unsafe_package", "encrypted",
         "general-purpose flag bit 0 (encryption) set on a member"),
        ("zip_bomb", make({"textures/bomb.png": bytes(64 * 1024 * 1024)}, deflated=frozenset({"textures/bomb.png"})),
         "resource_limit", "compression_ratio",
         "64 MiB of zeros in a tiny deflate stream (ratio > 200)"),
    ]


def _content_cases(desc: AssetDescriptorV1, prop: dict[str, bytes]) -> list[Case]:
    scene = prop["scenes/prop.tscn"].decode()
    shader_mat = ('[gd_resource type="ShaderMaterial" load_steps=2 format=3]\n\n'
                  '[ext_resource type="Shader" path="res://shaders/s.gdshader" id="1_sh"]\n\n'
                  '[resource]\nshader = ExtResource("1_sh")\n')
    script_tres = ('[gd_resource type="StandardMaterial3D" load_steps=2 format=3]\n\n'
                   '[ext_resource type="Script" path="res://materials/prop_mat.tres" id="1"]\n\n'
                   '[resource]\nscript = ExtResource("1")\n')

    def make(extra: dict[str, bytes], **kw: Any) -> bytes:
        return _make(desc, {**prop, **extra}, **kw)

    return [
        ("script_file", make({"scripts/script.gd": b"extends Node3D\n"}), "unsafe_package", "forbidden_extension",
         "a .gd script member"),
        ("tres_script_property", make({"materials/prop_mat.tres": script_tres.encode()}), "unsafe_package",
         "script_property", ".tres with script = ExtResource and a Script ext_resource"),
        ("connection_in_scene", make({"scenes/prop.tscn": (
            scene + '\n[connection signal="ready" from="." to="." method="_on_ready"]\n').encode()}),
         "unsafe_package", "connection", "scene declares a [connection] section"),
        ("custom_class_node", make({"scenes/prop.tscn": scene.replace('type="Marker3D"', 'type="MyProp"').encode()}),
         "unsafe_package", "type_not_allowed", "node type MyProp is not in the allowlist"),
        ("glb_external_buffer", make({"models/bad.glb": src.glb_with_external_uri()}), "unsafe_package",
         "glb_external_uri", "packaged .glb buffer uri points outside the file"),
        ("shader_include_escape", make({
            "materials/prop_mat.tres": shader_mat.encode(),
            "shaders/s.gdshader": b'shader_type spatial;\n#include "res://../outside.gdshaderinc"\n'}),
         "unsafe_package", "shader_include_escape", "#include leaves the package"),
        ("binary_scn", make({"scenes/binary.scn": b"RSRC\x00\x00\x00\x00"}), "unsafe_package", "binary_resource",
         "binary .scn is not Godot text format"),
        ("project_godot", make({"project.godot": b"config_version=5\n"}), "unsafe_package", "forbidden_file",
         "a project.godot member"),
    ]


def _manifest_cases(desc: AssetDescriptorV1, prop: dict[str, bytes]) -> list[Case]:
    scene = prop["scenes/prop.tscn"].decode()
    missing_scene = scene.replace(
        '[node name="GroundAnchor"',
        '[ext_resource type="PackedScene" path="res://deps/missing.glb" id="9_dep"]\n\n'
        '[node name="Extra" parent="." instance=ExtResource("9_dep")]\n\n[node name="GroundAnchor"')
    ghost = asset_key(SERVER, LIB_A, "ast_00000000000000ff", "ver_00000000000000v1")
    extra = b'[gd_resource type="BoxMesh" format=3]\n\n[resource]\n'
    return [
        ("undeclared_member", _make(desc, {**prop, "scenes/extra.tres": extra},
                                    undeclared=frozenset({"scenes/extra.tres"})),
         "unsafe_package", "undeclared_member", "member not listed in source_manifest.json files"),
        ("manifest_hash_mismatch", _make(desc, prop, tamper_hash=True), "integrity_mismatch", "sha256",
         "files[0].sha256 does not match the member bytes"),
        ("unavailable_dependency", _make(
            desc, {**prop, "scenes/prop.tscn": missing_scene.encode()},
            dep_paths={"res://deps/missing.glb": (ghost, "portable.glb")}),
         "unsupported_source_dependency", "unknown_asset_key",
         "resource_map names an asset_key that is not in asset_dependencies"),
    ]


def hostile_packages(d: Descs) -> list[Case]:
    desc, prop = d["primitive_prop"][0], src.prop_files()
    return _path_cases(desc, prop) + _content_cases(desc, prop) + _manifest_cases(desc, prop)
