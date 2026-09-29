"""Deterministic size transform of a static GLB: one new parent node, every other byte and JSON object untouched.

Why a wrapper node: scaling by editing vertices/accessors would rewrite the BIN chunk and could disturb normals,
tangents, texture payloads and material bindings. A single transform node above the scene roots scales the whole
asset exactly, keeps all payloads byte-identical and is trivially auditable (`preservation_checks`).
"""
from __future__ import annotations

import json
import math
import struct
from typing import Any

import numpy as np
from assetstudio_core.variants import AxisScale, GlbTransform, TargetHeight, UniformScale
from pydantic import TypeAdapter, ValidationError

from .glb import GlbRejected, inspect_container

ALLOWED_REQUIRED = frozenset({"KHR_texture_transform", "KHR_materials_emissive_strength", "KHR_materials_unlit"})
MAX_TRANSFORMED_VERTICES = 20_000_000
MAX_NODES_VISITED = 1_000_000
MAX_DEPTH = 256
WRAPPER_NAME = "assetstudio_variant_transform"
MIN_EFFECTIVE_SCALE, MAX_EFFECTIVE_SCALE = 1e-6, 1e6
FLOAT, TRIANGLES = 5126, 4


class TransformRejected(ValueError):
    """`code`: unsupported_source_features | corrupt_source | invalid_transform."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _unsupported(msg: str) -> TransformRejected:
    return TransformRejected("unsupported_source_features", msg)


def _corrupt(msg: str) -> TransformRejected:
    return TransformRejected("corrupt_source", msg)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite JSON constant {name}")


def _split(data: bytes) -> tuple[dict[str, Any], int, int]:
    """Validated (json doc, end of JSON chunk, BIN payload length); BIN is the chunk after JSON, if any."""
    try:
        inspect_container(data)
    except GlbRejected as e:
        raise (_unsupported(str(e)) if "URI" in str(e) else _corrupt(str(e))) from e
    json_len = struct.unpack_from("<I", data, 12)[0]
    try:
        doc = json.loads(data[20:20 + json_len], parse_constant=_reject_constant)
    except ValueError as e:
        raise _corrupt(f"invalid JSON chunk: {e}") from e
    end, bin_len = 20 + json_len, 0
    if end + 8 <= len(data):
        clen, ctype = struct.unpack_from("<I4s", data, end)
        if ctype == b"BIN\x00" and end + 8 + clen <= len(data):
            bin_len = clen
    return doc, end, bin_len


def _check_features(doc: dict[str, Any]) -> None:
    for what in ("skins", "animations"):
        if doc.get(what):
            raise _unsupported(f"{what} present; only static models can be scaled")
    bad = sorted(set(doc.get("extensionsRequired") or []) - ALLOWED_REQUIRED)
    if bad:
        raise _unsupported(f"required extension(s) not supported: {', '.join(bad)}")
    buffers = doc.get("buffers") or []
    if len(buffers) != 1:
        raise _unsupported(f"expected exactly one (GLB BIN) buffer, found {len(buffers)}")
    if any("bufferView" not in im for im in doc.get("images") or []):
        raise _unsupported("images must be embedded through bufferView")
    for mi, mesh in enumerate(doc.get("meshes") or []):
        for pi, prim in enumerate(mesh.get("primitives") or []):
            where = f"meshes[{mi}].primitives[{pi}]"
            if prim.get("targets"):
                raise _unsupported(f"{where} has morph targets")
            if prim.get("mode", TRIANGLES) != TRIANGLES:
                raise _unsupported(f"{where} mode {prim.get('mode')} is not triangles")
            if "POSITION" not in (prim.get("attributes") or {}):
                raise _unsupported(f"{where} has no POSITION")
    for ni, node in enumerate(doc.get("nodes") or []):
        if node.get("skin") is not None or node.get("weights"):
            raise _unsupported(f"nodes[{ni}] uses skin or morph weights")


def _positions(doc: dict[str, Any], blob: memoryview, prim: dict[str, Any], where: str) -> np.ndarray:
    """(N, 3) float64 POSITION values honouring accessor/bufferView offsets and byteStride."""
    try:
        acc = doc["accessors"][prim["attributes"]["POSITION"]]
    except (KeyError, IndexError, TypeError) as e:
        raise _corrupt(f"{where}: POSITION accessor missing") from e
    if acc.get("sparse"):
        raise _unsupported(f"{where}: sparse POSITION accessor")
    if acc.get("componentType") != FLOAT or acc.get("type") != "VEC3" or acc.get("normalized"):
        raise _unsupported(f"{where}: POSITION must be FLOAT VEC3 (quantized positions are unsupported)")
    count = acc.get("count")
    if not isinstance(count, int) or count < 1 or "bufferView" not in acc:
        raise _corrupt(f"{where}: POSITION accessor has no data")
    try:
        bv = doc["bufferViews"][acc["bufferView"]]
    except (KeyError, IndexError, TypeError) as e:
        raise _corrupt(f"{where}: bad bufferView") from e
    if bv.get("buffer", 0) != 0:
        raise _corrupt(f"{where}: bufferView not in the GLB buffer")
    stride = bv.get("byteStride") or 12
    start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    span = (count - 1) * stride + 12
    view_end = bv.get("byteOffset", 0) + bv.get("byteLength", 0)
    if stride < 12 or start < 0 or start + span > min(len(blob), view_end):
        raise _corrupt(f"{where}: POSITION data outside its buffer view")
    raw = np.frombuffer(blob, dtype=np.uint8, count=span, offset=start)
    rows = np.lib.stride_tricks.as_strided(raw, shape=(count, 12), strides=(stride, 1))
    pts = np.ascontiguousarray(rows).view("<f4").astype(np.float64).reshape(count, 3)
    if not np.isfinite(pts).all():
        raise _corrupt(f"{where}: non-finite POSITION values")
    return pts


def _local_matrix(node: dict[str, Any], where: str) -> np.ndarray:
    if "matrix" in node:
        m = np.array(node["matrix"], dtype=np.float64)
        if m.shape != (16,) or not np.isfinite(m).all():
            raise _corrupt(f"{where}: invalid matrix")
        return m.reshape(4, 4).T
    t = np.array(node.get("translation", [0, 0, 0]), dtype=np.float64)
    q = np.array(node.get("rotation", [0, 0, 0, 1]), dtype=np.float64)
    s = np.array(node.get("scale", [1, 1, 1]), dtype=np.float64)
    if t.shape != (3,) or q.shape != (4,) or s.shape != (3,) or not np.isfinite(np.r_[t, q, s]).all():
        raise _corrupt(f"{where}: invalid TRS")
    n = np.linalg.norm(q)
    if n < 1e-12:
        raise _corrupt(f"{where}: zero rotation quaternion")
    x, y, z, w = q / n
    rot = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                    [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                    [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    m = np.eye(4)
    m[:3, :3] = rot * s
    m[:3, 3] = t
    return m


def _scene_roots(doc: dict[str, Any]) -> tuple[int, list[int]]:
    scenes = doc.get("scenes") or []
    if not scenes:
        raise _corrupt("no scenes")
    idx = doc.get("scene")
    if idx is None:
        if len(scenes) > 1:
            raise _unsupported("multiple scenes without a default scene")
        idx = 0
    if not isinstance(idx, int) or not 0 <= idx < len(scenes):
        raise _corrupt("default scene index out of range")
    return idx, list(scenes[idx].get("nodes") or [])


def inspect_static_glb(data: bytes) -> dict[str, Any]:
    """Reject anything a wrapper-node scale could not preserve; else report exact world-space bounds.

    Bounds transform every referenced vertex through its node's full world matrix (instances count separately).
    """
    doc, end, bin_len = _split(data)
    _check_features(doc)
    scene, roots = _scene_roots(doc)
    blob = memoryview(data)[end + 8:end + 8 + bin_len]
    nodes = doc.get("nodes") or []
    meshes = doc.get("meshes") or []
    cache: dict[int, list[np.ndarray]] = {}
    lo, hi, total, visited = np.full(3, np.inf), np.full(3, -np.inf), 0, 0
    stack = [(r, np.eye(4), 1) for r in roots]
    while stack:
        ni, parent, depth = stack.pop()
        visited += 1
        if not isinstance(ni, int) or not 0 <= ni < len(nodes):
            raise _corrupt(f"node index {ni} out of range")
        if depth > MAX_DEPTH or visited > MAX_NODES_VISITED:
            raise _corrupt("node hierarchy too deep, cyclic or too large")
        node = nodes[ni]
        world = parent @ _local_matrix(node, f"nodes[{ni}]")
        stack.extend((c, world, depth + 1) for c in node.get("children") or [])
        mi = node.get("mesh")
        if mi is None:
            continue
        if not isinstance(mi, int) or not 0 <= mi < len(meshes):
            raise _corrupt(f"nodes[{ni}].mesh out of range")
        if mi not in cache:
            cache[mi] = [_positions(doc, blob, p, f"meshes[{mi}].primitives[{pi}]")
                         for pi, p in enumerate(meshes[mi].get("primitives") or [])]
        for pts in cache[mi]:
            total += len(pts)
            if total > MAX_TRANSFORMED_VERTICES:
                raise _unsupported(f"more than {MAX_TRANSFORMED_VERTICES} vertices to transform")
            w = pts @ world[:3, :3].T + world[:3, 3]
            lo, hi = np.minimum(lo, w.min(0)), np.maximum(hi, w.max(0))
    if total == 0 or not (np.isfinite(lo).all() and np.isfinite(hi).all()):
        raise _corrupt("scene has no geometry")
    if not (hi > lo).any():
        raise _corrupt("degenerate bounds: all vertices coincide")
    return {"scene": scene, "roots": roots, "nodes": len(nodes), "meshes": len(meshes),
            "materials": len(doc.get("materials") or []), "textures": len(doc.get("textures") or []),
            "extensions_used": list(doc.get("extensionsUsed") or []),
            "alpha_modes": sorted({m.get("alphaMode", "OPAQUE") for m in doc.get("materials") or []}),
            "vertex_count": total, "bounds": {"min": lo.tolist(), "max": hi.tolist()}}


def _coerce(transform: Any) -> UniformScale | AxisScale | TargetHeight:
    if isinstance(transform, (UniformScale, AxisScale, TargetHeight)):
        return transform
    try:
        return TypeAdapter(GlbTransform).validate_python(transform)
    except ValidationError as e:
        raise TransformRejected("invalid_transform", str(e)) from e


def _plan(t: UniformScale | AxisScale | TargetHeight, lo: np.ndarray, hi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(scale xyz, anchor point in world space)."""
    if isinstance(t, UniformScale):
        s = np.full(3, t.factor)
    elif isinstance(t, AxisScale):
        s = np.array([t.x, t.y, t.z])
    else:
        if not t.units_confirmed:
            raise TransformRejected("invalid_transform", "target height needs confirmed units (1 unit = 1 m)")
        if hi[1] - lo[1] <= 0:
            raise _corrupt("source has zero height")
        s = np.full(3, t.height_m / (hi[1] - lo[1]))
    if not np.isfinite(s).all() or ((s < MIN_EFFECTIVE_SCALE) | (s > MAX_EFFECTIVE_SCALE)).any():
        raise TransformRejected("invalid_transform", f"effective scale {s.tolist()} outside supported range")
    c = (lo + hi) / 2
    p = {"source_origin": np.zeros(3), "bounds_center": c, "bottom_center": np.array([c[0], lo[1], c[2]])}[t.anchor]
    return s, p


def _rewrite(data: bytes, doc: dict[str, Any]) -> bytes:
    """New JSON chunk (space padded) + every following byte verbatim; only header lengths change."""
    old_len = struct.unpack_from("<I", data, 12)[0]
    body = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    body += b" " * (-len(body) % 4)
    rest = data[20 + old_len:]
    total = 12 + 8 + len(body) + len(rest)
    return struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<I4s", len(body), b"JSON") + body + rest


def apply_glb_transform(data: bytes, transform: Any) -> tuple[bytes, dict[str, Any]]:
    """Scale about a world-space anchor via one wrapper node: M = T(p) S T(-p). Returns (GLB bytes, report)."""
    t = _coerce(transform)
    src = inspect_static_glb(data)
    lo, hi = np.array(src["bounds"]["min"]), np.array(src["bounds"]["max"])
    s, p = _plan(t, lo, hi)
    m = np.eye(4)
    m[:3, :3] = np.diag(s)
    m[:3, 3] = p - s * p
    if not np.isfinite(m).all() or np.linalg.det(m) <= 0:
        raise TransformRejected("invalid_transform", "transform is not a finite proper (non-reflecting) scale")
    doc, _, _ = _split(data)
    nodes = doc.setdefault("nodes", [])
    nodes.append({"name": WRAPPER_NAME, "matrix": [float(v) for v in m.T.reshape(16)], "children": list(src["roots"])})
    doc["scenes"][src["scene"]]["nodes"] = [len(nodes) - 1]
    out = _rewrite(data, doc)
    res = inspect_static_glb(out)
    ilo, ihi = np.array(src["bounds"]["min"]), np.array(src["bounds"]["max"])
    olo, ohi = np.array(res["bounds"]["min"]), np.array(res["bounds"]["max"])
    tol = max(1e-5, t.height_m * 1e-5) if isinstance(t, TargetHeight) else 1e-5 * float(np.max(ohi - olo))
    report: dict[str, Any] = {
        "transform": t.model_dump(), "effective_scale": s.tolist(), "anchor": t.anchor, "anchor_point": p.tolist(),
        "input_bounds": src["bounds"], "output_bounds": res["bounds"],
        "input_height": float(ihi[1] - ilo[1]), "output_height": float(ohi[1] - olo[1]), "tolerance": tol}
    if isinstance(t, TargetHeight):
        report["height_ok"] = abs(report["output_height"] - t.height_m) <= tol
    return out, report


def preservation_checks(src: bytes, out: bytes) -> list[dict[str, Any]]:
    """Independent proof that only the wrapper node and the scene's root list changed."""
    a, a_end, _ = _split(src)
    b, b_end, _ = _split(out)
    scene, roots = _scene_roots(a)
    a_nodes, b_nodes = a.get("nodes") or [], b.get("nodes") or []
    new = b_nodes[-1] if len(b_nodes) == len(a_nodes) + 1 else {}
    others = lambda d: {k: v for k, v in d.items() if k not in ("nodes", "scenes")}  # noqa: E731
    scenes_a, scenes_b = a.get("scenes") or [], b.get("scenes") or []
    no_nodes = lambda s: {k: v for k, v in s.items() if k != "nodes"}  # noqa: E731
    rest_ok = len(scenes_a) == len(scenes_b) and all(
        (x == y) if i != scene else (no_nodes(x) == no_nodes(y))
        for i, (x, y) in enumerate(zip(scenes_a, scenes_b, strict=False)))
    sel = lambda d, keys: {k: d.get(k) for k in keys}  # noqa: E731
    render_keys = ("materials", "textures", "samplers", "images")
    ext_keys = ("extensionsUsed", "extensionsRequired", "extensions")

    def chk(cid: str, ok: bool, detail: str) -> dict[str, Any]:
        return {"id": cid, "ok": bool(ok), "detail": detail}

    return [
        chk("bin_identical", src[a_end:] == out[b_end:], "BIN chunk and trailing bytes byte-identical"),
        chk("json_objects_unchanged", others(a) == others(b) and a_nodes == b_nodes[:len(a_nodes)] and rest_ok,
            "all top-level objects except nodes/scenes equal; original nodes equal element-wise"),
        chk("single_parent_added", len(b_nodes) == len(a_nodes) + 1 and new.get("name") == WRAPPER_NAME
            and set(new) == {"name", "matrix", "children"}, "exactly one wrapper node appended"),
        chk("scene_roots_wrapped", new.get("children") == roots
            and (b.get("scenes") or [{}])[scene].get("nodes") == [len(a_nodes)],
            "scene root list is [wrapper] over old roots"),
        chk("alpha_and_sampler_unchanged", sel(a, render_keys) == sel(b, render_keys),
            "materials (alphaMode, doubleSided, bindings), textures, samplers, images equal"),
        chk("extensions_unchanged", sel(a, ext_keys) == sel(b, ext_keys), "extensionsUsed/Required/extensions equal"),
        chk("wrapper_matrix_finite", all(math.isfinite(v) for v in new.get("matrix", [math.nan])), "matrix finite"),
    ]
