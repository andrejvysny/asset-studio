"""Static scene structure of a source package: node paths, mesh surface counts, collision shapes, cycle checks.

Everything is derived from parsed text only; subtrees that come from .glb files or asset dependencies are opaque.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .godot_text import GodotTextDoc, Ref
from .source_report import SourcePackageError

MAX_INSTANCE_DEPTH = 32
MAX_NODES = 65536
PRIMITIVE_MESHES = frozenset({"BoxMesh", "CylinderMesh", "SphereMesh", "PlaneMesh", "QuadMesh", "PrismMesh",
                              "CapsuleMesh", "TorusMesh"})
SHAPE_TYPES = {"BoxShape3D": "box", "SphereShape3D": "sphere", "CapsuleShape3D": "capsule",
               "CylinderShape3D": "cylinder", "ConvexPolygonShape3D": "convex", "ConcavePolygonShape3D": "concave"}


@dataclass(frozen=True)
class NodeDef:
    """One [node] of a single scene file, before instancing is expanded."""
    name: str
    parent: str | None
    type: str | None
    instance: str | None  # package path (.tscn/.glb) or "dep:<key>"
    surfaces: int | None
    shape: str | None  # CollisionShape3D only; None = unknown


@dataclass(frozen=True)
class NodeInfo:
    type: str | None
    surfaces: int | None  # None = not verifiable (CSG, external or unparseable mesh)


@dataclass
class SceneStructure:
    nodes: dict[str, NodeInfo] = field(default_factory=dict)  # node path ("." = root) -> info
    opaque: list[str] = field(default_factory=list)  # path prefixes whose content comes from .glb / dependencies
    shapes: list[str | None] = field(default_factory=list)  # one entry per CollisionShape3D; None = unknown type

    def under_opaque(self, node_path: str) -> bool:
        return any(node_path == p or node_path.startswith(p + "/") for p in self.opaque)


def scene_defs(doc: GodotTextDoc, targets: dict[str, str | None]) -> list[NodeDef]:
    sub_types = {s.attrs.get("id"): s for s in doc.sub_resources()}
    out = []
    for sec in doc.nodes():
        name, parent, ntype = sec.attrs.get("name"), sec.attrs.get("parent"), sec.attrs.get("type")
        if not isinstance(name, str) or not (parent is None or isinstance(parent, str)):
            continue
        inst = sec.attrs.get("instance")
        target = targets.get(inst.id) if isinstance(inst, Ref) and inst.kind == "ExtResource" else None
        props = dict(sec.props)
        ntype = ntype if isinstance(ntype, str) else None
        out.append(NodeDef(name, parent, ntype, target, _surface_count(ntype, props, sub_types),
                           _shape_of(props, sub_types) if ntype == "CollisionShape3D" else None))
    return out


def _sub_of(value: Any, subs: dict[Any, Any]) -> Any:
    return subs.get(value.id) if isinstance(value, Ref) and value.kind == "SubResource" else None


def _surface_count(ntype: str | None, props: dict[str, Any], subs: dict[Any, Any]) -> int | None:
    if ntype != "MeshInstance3D":
        return None
    mesh = _sub_of(props.get("mesh"), subs)
    mtype = mesh.attrs.get("type") if mesh is not None else None
    if mtype in PRIMITIVE_MESHES:
        return 1
    if mtype == "ArrayMesh":
        surfaces = dict(mesh.props).get("_surfaces")
        return len(surfaces) if isinstance(surfaces, list) else None
    return None


def _shape_of(props: dict[str, Any], subs: dict[Any, Any]) -> str | None:
    shape = _sub_of(props.get("shape"), subs)
    return SHAPE_TYPES.get(shape.attrs.get("type")) if shape is not None else None


def _join(base: str, local: str) -> str:
    return local if base == "." else f"{base}/{local}"


class _Expander:
    def __init__(self, defs: dict[str, list[NodeDef]]) -> None:
        self.defs, self.out = defs, SceneStructure()

    def expand(self, scene: str, base: str, depth: int) -> None:
        if depth > MAX_INSTANCE_DEPTH:
            raise SourcePackageError("resource_limit", "structure_limit",
                                     f"scene instancing deeper than {MAX_INSTANCE_DEPTH}", scene)
        have_root = False
        for d in self.defs.get(scene, ()):
            if d.parent is None:
                if have_root:
                    continue
                have_root, path = True, base
            else:
                path = _join(base, d.name if d.parent == "." else f"{d.parent}/{d.name}")
            self._node(d, path, depth)

    def _node(self, d: NodeDef, path: str, depth: int) -> None:
        if len(self.out.nodes) >= MAX_NODES:
            raise SourcePackageError("resource_limit", "structure_limit", f"more than {MAX_NODES} expanded nodes")
        if d.instance is not None and d.instance in self.defs:
            self.expand(d.instance, path, depth + 1)
            return
        if d.instance is not None:
            self.out.opaque.append(path)
        self.out.nodes[path] = NodeInfo(d.type, d.surfaces)
        if d.type == "CollisionShape3D":
            self.out.shapes.append(d.shape)


def build_structure(defs: dict[str, list[NodeDef]], entry: str) -> SceneStructure | None:
    """Expanded tree of `entry`; the scene graph must be acyclic (checked by the caller)."""
    if entry not in defs:
        return None
    ex = _Expander(defs)
    ex.expand(entry, ".", 0)
    return ex.out


def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """One cycle per back edge found by an iterative DFS (white/grey/black)."""
    state: dict[str, int] = {}
    cycles: list[list[str]] = []
    for start in sorted(graph):
        if start in state:
            continue
        stack = [(start, iter(sorted(graph.get(start, ()))))]
        state[start] = 1
        trail = [start]
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[node] = 2
                stack.pop()
                trail.pop()
            elif state.get(nxt) == 1:
                cycles.append([*trail[trail.index(nxt):], nxt])
            elif nxt not in state:
                state[nxt] = 1
                trail.append(nxt)
                stack.append((nxt, iter(sorted(graph.get(nxt, ())))))
    return cycles
