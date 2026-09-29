"""CPU renders of a GLB (2x2 orbit previews, neutral reference views): base colour texture x factor x vertex colour,
alpha MASK (BLEND approximated as MASK at 0.5), two-sided Lambert. NumPy z-buffer; no GPU, no NC code.

Screen arrays are (H, W); triangles are rasterised at pixel centres, nearest surviving fragment wins.
Fragment arrays: face index (N,), pixel (N,), barycentric weights (N, 3), depth (N,).
"""
from __future__ import annotations

import io
import math

import numpy as np
import trimesh
from PIL import Image

from .render_materials import (
    MeshMaterial,
    RenderUnsupported,
    check_required,
    gltf_json,
    gltf_warnings,
    material_of,
)

__all__ = ["RenderUnsupported"]

BG = np.array([38, 39, 42], dtype=np.float32)
MAX_CANDIDATES = 1 << 24


def _load(data: bytes) -> list[trimesh.Trimesh]:
    scene = trimesh.load(io.BytesIO(data), file_type="glb", force="scene")
    return [g for g in scene.dump(concatenate=False) if isinstance(g, trimesh.Trimesh) and len(g.faces)]


def _camera(yaw: float, pitch: float) -> np.ndarray:
    cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]])
    return rx @ ry


def _candidates(xy: np.ndarray, size: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Pixel centres inside each screen triangle's bounding box: (face index, px, py)."""
    lo = np.clip(np.ceil(xy.min(1) - 0.5), 0, size - 1).astype(np.int64)
    hi = np.clip(np.floor(xy.max(1) - 0.5), 0, size - 1).astype(np.int64)
    wh = np.maximum(hi - lo + 1, 0)
    counts = wh[:, 0] * wh[:, 1]
    if counts.sum() > MAX_CANDIDATES:
        raise ValueError("preview raster budget exceeded")
    idx = np.repeat(np.arange(len(xy)), counts)
    local = np.arange(counts.sum()) - np.repeat(np.cumsum(counts) - counts, counts)
    return idx, lo[idx, 0] + local % wh[idx, 0], lo[idx, 1] + local // wh[idx, 0]


def _raster(mesh: trimesh.Trimesh, rot: np.ndarray, scale: float, center: np.ndarray, size: int,
            ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Fragments inside triangles: (faces (F, 3), face idx (N,), linear pixel (N,), weights (N, 3), depth (N,))."""
    v = (np.asarray(mesh.vertices, dtype=np.float64) - center) @ rot.T
    xy_all = np.stack([v[:, 0] * scale + size / 2, -v[:, 1] * scale + size / 2], -1)
    faces = np.asarray(mesh.faces)
    xy = xy_all[faces]  # (F, 3, 2)
    idx, px, py = _candidates(xy, size)
    a, b, c = xy[idx, 0], xy[idx, 1], xy[idx, 2]
    p = np.stack([px + 0.5, py + 0.5], -1)
    v0, v1, v2 = b - a, c - a, p - a
    den = v0[:, 0] * v1[:, 1] - v1[:, 0] * v0[:, 1]
    ok = np.abs(den) > 1e-12
    den = np.where(ok, den, 1.0)
    l1 = (v2[:, 0] * v1[:, 1] - v1[:, 0] * v2[:, 1]) / den
    l2 = (v0[:, 0] * v2[:, 1] - v2[:, 0] * v0[:, 1]) / den
    l0 = 1 - l1 - l2
    inside = ok & (l0 >= 0) & (l1 >= 0) & (l2 >= 0)
    idx, w = idx[inside], np.stack([l0, l1, l2], -1)[inside]
    z = (v[faces[idx]][:, :, 2] * w).sum(1)
    return faces, idx, (py * size + px)[inside], w, z


def _nearest(lin: np.ndarray, z: np.ndarray, depth: np.ndarray) -> np.ndarray:
    """Positions of the fragments that win their pixel (nearest = largest z) and beat the buffer; updates depth."""
    order = np.lexsort((-z, lin))
    ls = lin[order]
    first = np.ones(len(ls), dtype=bool)
    first[1:] = ls[1:] != ls[:-1]
    sel = order[first]
    sel = sel[z[sel] > depth.ravel()[lin[sel]]]
    depth.ravel()[lin[sel]] = z[sel]
    return sel


def _shade(mesh: trimesh.Trimesh, rot: np.ndarray, idx: np.ndarray) -> np.ndarray:
    """Two-sided Lambert (N,): the normal is flipped to face the camera (+z) so back faces are lit, not culled."""
    n = (np.asarray(mesh.face_normals) @ rot.T)[idx]
    n = np.where(n[:, 2:3] < 0, -n, n)
    return 0.35 + 0.65 * np.clip(n @ np.array([0.3, 0.5, 0.81]), 0, 1)


def _draw(mesh: trimesh.Trimesh, rot: np.ndarray, scale: float, center: np.ndarray, size: int,
          color: np.ndarray, depth: np.ndarray, mat: MeshMaterial | None = None) -> None:
    mat = mat or material_of(mesh)
    faces, idx, lin, w, z = _raster(mesh, rot, scale, center, size)
    rgba = None
    if mat.discards:  # discarded fragments must not occlude what is behind them: filter before depth selection
        rgba = mat.sample(faces, idx, w)
        keep = rgba[:, 3] >= mat.effective_cutoff
        idx, lin, w, z, rgba = idx[keep], lin[keep], w[keep], z[keep], rgba[keep]
    sel = _nearest(lin, z, depth)
    idx, lin, w = idx[sel], lin[sel], w[sel]
    rgba = mat.sample(faces, idx, w) if rgba is None else rgba[sel]
    color.reshape(-1, 3)[lin] = rgba[:, :3] * 255 * _shade(mesh, rot, idx)[:, None]


def preview_png(data: bytes, size: int = 384) -> bytes:
    """2x2 grid (front-left, front-right, back-right, back-left), 20° elevation, shared framing."""
    meshes = _load(data)
    if not meshes:
        raise ValueError("no triangles to render")
    pts = np.concatenate([np.asarray(m.vertices) for m in meshes])
    center = (pts.min(0) + pts.max(0)) / 2
    radius = float(np.linalg.norm(pts - center, axis=1).max()) or 1.0
    scale = size * 0.46 / radius
    tiles = []
    for yaw in (-45, 45, 135, 225):
        rot = _camera(math.radians(yaw), math.radians(20))
        color = np.tile(BG, (size, size, 1))
        depth = np.full((size, size), -np.inf)
        for m in meshes:
            _draw(m, rot, scale, center, size, color, depth)
        tiles.append(np.clip(color, 0, 255).astype(np.uint8))
    grid = np.concatenate([np.concatenate(tiles[:2], 1), np.concatenate(tiles[2:], 1)], 0)
    out = io.BytesIO()
    Image.fromarray(grid).save(out, "PNG", optimize=True)
    return out.getvalue()


def glb_stats(data: bytes) -> dict[str, object]:
    """Delivered-mesh facts for budgets and the version record (welded topology, see mesh.geometric_components)."""
    from .mesh import geometric_components

    meshes = _load(data)
    tri = sum(len(m.faces) for m in meshes)
    comps = sum(int(geometric_components(m).max() + 1) for m in meshes)
    pts = np.concatenate([np.asarray(m.vertices) for m in meshes]) if meshes else np.zeros((1, 3))
    return {"triangles": int(tri), "vertices": int(sum(len(m.vertices) for m in meshes)), "components": comps,
            "extents": (pts.max(0) - pts.min(0)).round(5).tolist(), "meshes": len(meshes)}


RENDERER_ID = "assetstudio.cpu_lambert.v2"
REFERENCE_VIEWS: dict[str, tuple[float, float]] = {"three_quarter": (-45, 20), "rear": (135, 20), "side": (90, 10)}


def render_view(data: bytes, yaw_deg: float, pitch_deg: float, size: int = 1024,
                background: tuple[int, int, int] = (200, 200, 200), margin: float = 0.08) -> tuple[bytes, dict]:
    """One neutral object view (flat background, no floor/shadow) framed by the bounding sphere plus margin.

    Meant as image-edit conditioning, not a faithful material preview: warnings list what is approximated or
    ignored. Raises RenderUnsupported when the GLB requires features that cannot be honoured.
    """
    doc = gltf_json(data)
    check_required(doc)
    meshes = _load(data)
    if not meshes:
        raise ValueError("no triangles to render")
    mats = [material_of(m) for m in meshes]
    pts = np.concatenate([np.asarray(m.vertices) for m in meshes])
    center = (pts.min(0) + pts.max(0)) / 2
    radius = float(np.linalg.norm(pts - center, axis=1).max()) or 1.0
    scale = size * (0.5 - margin) / radius
    color = np.tile(np.array(background, dtype=np.float32), (size, size, 1))
    depth = np.full((size, size), -np.inf)
    rot = _camera(math.radians(yaw_deg), math.radians(pitch_deg))
    for m, mat in zip(meshes, mats, strict=True):
        _draw(m, rot, scale, center, size, color, depth, mat)
    out = io.BytesIO()
    Image.fromarray(np.clip(color, 0, 255).astype(np.uint8)).save(out, "PNG", optimize=True)
    return out.getvalue(), {"yaw": yaw_deg, "pitch": pitch_deg, "size": size, "background": list(background),
                            "renderer": RENDERER_ID, "culling": "none", "lighting": "two-sided Lambert",
                            "shading": "base colour texture x factor x vertex colour; alpha MASK/BLEND as cutout",
                            "warnings": sorted(gltf_warnings(doc, mats))}


def reference_views(data: bytes, size: int = 1024) -> dict[str, tuple[bytes, dict]]:
    return {name: render_view(data, yaw, pitch, size) for name, (yaw, pitch) in REFERENCE_VIEWS.items()}
