"""Mesh cleanup, stats and export validation. Pure trimesh/numpy (CPU-testable, no torch).

Exported meshes are UV-split: chart seams duplicate vertices, so vertex-index adjacency sees every UV
chart as a separate component. All topology questions are answered on a position-welded copy;
the delivered mesh itself is never welded (that would destroy UV seams).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import trimesh
from PIL import Image


def geometric_components(mesh: trimesh.Trimesh) -> np.ndarray:
    """Per-face component label computed on a position-welded copy. Face order is preserved."""
    diag = trimesh.Trimesh(vertices=np.asarray(mesh.vertices).copy(), faces=np.asarray(mesh.faces).copy(), process=False)
    diag.merge_vertices()
    return trimesh.graph.connected_component_labels(diag.face_adjacency, node_count=len(diag.faces))


def drop_floaters(mesh: trimesh.Trimesh, min_ratio: float) -> tuple[trimesh.Trimesh, int]:
    """Remove geometric components with < min_ratio of faces. The largest component is always kept."""
    if len(mesh.faces) == 0:
        raise ValueError("mesh is empty before cleanup")
    labels = geometric_components(mesh)
    counts = np.bincount(labels)
    keep_labels = set(np.nonzero(counts >= min_ratio * len(mesh.faces))[0].tolist()) | {int(np.argmax(counts))}
    if len(keep_labels) == len(counts):
        return mesh, 0
    keep = np.isin(labels, list(keep_labels))
    mesh.update_faces(keep)
    mesh.remove_unreferenced_vertices()
    if len(mesh.faces) == 0:
        raise ValueError("floater removal produced an empty mesh")
    return mesh, int(len(counts) - len(keep_labels))


def _material(mesh: trimesh.Trimesh) -> object | None:
    return getattr(mesh.visual, "material", None)


def mesh_info(mesh: trimesh.Trimesh, glb_path: Path, removed_components: int) -> dict:
    if len(mesh.faces) == 0:
        raise ValueError("mesh is empty")
    labels = geometric_components(mesh)
    material = _material(mesh)
    uv = getattr(mesh.visual, "uv", None)
    return {
        "triangles": int(len(mesh.faces)),
        "vertices": int(len(mesh.vertices)),
        "components": int(labels.max() + 1),
        "removed_floater_components": removed_components,
        "bbox_min": mesh.bounds[0].round(5).tolist(),
        "bbox_max": mesh.bounds[1].round(5).tolist(),
        "extents": mesh.extents.round(5).tolist(),
        "watertight": bool(mesh.is_watertight),
        "has_uv": uv is not None and len(uv) == len(mesh.vertices),
        "has_base_color_texture": getattr(material, "baseColorTexture", None) is not None,
        "has_metallic_roughness_texture": getattr(material, "metallicRoughnessTexture", None) is not None,
        "file_size_bytes": glb_path.stat().st_size,
    }


def save_textures(mesh: trimesh.Trimesh, out_dir: Path) -> list[str]:
    material = _material(mesh)
    saved: list[str] = []
    for name in ("baseColorTexture", "metallicRoughnessTexture"):
        tex = getattr(material, name, None)
        if isinstance(tex, Image.Image):
            out_dir.mkdir(parents=True, exist_ok=True)
            tex.save(out_dir / f"{name}.png")
            saved.append(f"{name}.png")
    return saved


def _check(checks: list[dict], cid: str, ok: bool, detail: str = "") -> bool:
    checks.append({"id": cid, "ok": bool(ok), "detail": detail})
    return bool(ok)


def validate_glb(path: Path) -> dict:
    """Reload the delivered file and check what a consumer needs. ok only if every check passes."""
    checks: list[dict] = []
    try:
        scene = trimesh.load(path, force="scene")
        geoms = [g for g in scene.geometry.values() if isinstance(g, trimesh.Trimesh)]
        _check(checks, "reload", bool(geoms), f"{len(geoms)} mesh(es)")
    except Exception as e:  # any loader failure is a validation failure, not a crash
        _check(checks, "reload", False, f"{type(e).__name__}: {e}")
        return {"ok": False, "checks": checks}
    if not geoms:
        return {"ok": False, "checks": checks}
    faces = sum(len(g.faces) for g in geoms)
    _check(checks, "non_empty", faces > 0, f"{faces} triangles")
    finite = all(np.isfinite(np.asarray(g.vertices)).all() for g in geoms)
    _check(checks, "finite_vertices", finite)
    valid_idx = all(len(g.faces) == 0 or (g.faces.min() >= 0 and g.faces.max() < len(g.vertices)) for g in geoms)
    _check(checks, "valid_indices", valid_idx)
    has_uv = all(getattr(g.visual, "uv", None) is not None and len(g.visual.uv) == len(g.vertices) for g in geoms)
    _check(checks, "uvs_present", has_uv)
    has_tex = all(getattr(_material(g), "baseColorTexture", None) is not None for g in geoms)
    _check(checks, "material_texture_present", has_tex)
    return {"ok": all(c["ok"] for c in checks), "checks": checks}
