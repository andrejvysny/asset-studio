"""Material model for the CPU reference renderer: what a glTF material contributes to a fragment colour, and the
honest list of features the renderer cannot reproduce. Colours are float32 in 0..1; textures stay uint8 until sampled.

Array shapes: texture (H, W, 4) uint8 RGBA; uv (V, 2); per-fragment barycentric weights (N, 3); fragment RGBA (N, 4).
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any

import numpy as np
import trimesh

from .glb import GlbRejected, check_document_shape

UNTEXTURED = np.float32(190 / 255)  # flat grey base when a material has no colour at all
SUPPORTED_EXTENSIONS = frozenset({"KHR_materials_emissive_strength", "KHR_materials_ior", "KHR_materials_specular",
                                  "KHR_lights_punctual", "KHR_materials_variants"})
UNSUPPORTED_WRAP = (33071, 33648)  # CLAMP_TO_EDGE, MIRRORED_REPEAT: only REPEAT is sampled
DEFAULT_CUTOFF = 0.5


class RenderUnsupported(ValueError):
    """The GLB requires features the renderer cannot honour; a wrong picture would be worse than none."""
    code = "unsupported_source_features"


@dataclass
class MeshMaterial:
    tex: np.ndarray | None  # (H, W, 4) uint8, only when UVs are usable
    uv: np.ndarray | None  # (V, 2) float32
    factor: np.ndarray  # (4,) float32 RGBA
    vertex_rgba: np.ndarray | None  # (V, 4) float32
    alpha_mode: str  # OPAQUE | MASK | BLEND
    cutoff: float
    texture_referenced: bool
    coloured: bool  # any factor / vertex colour differs from white

    @property
    def discards(self) -> bool:
        return self.alpha_mode in ("MASK", "BLEND")

    @property
    def effective_cutoff(self) -> float:
        return self.cutoff if self.alpha_mode == "MASK" else DEFAULT_CUTOFF

    def sample(self, faces: np.ndarray, idx: np.ndarray, w: np.ndarray) -> np.ndarray:
        """RGBA (N, 4) float32 for fragments of face idx (N,) with barycentric weights w (N, 3)."""
        n = len(idx)
        rgba = np.ones((n, 4), np.float32)
        if self.tex is not None and self.uv is not None:
            t = (self.uv[faces[idx]] * w[..., None]).sum(1)
            th, tw = self.tex.shape[:2]
            tx = np.clip(np.floor((t[:, 0] % 1.0) * tw), 0, tw - 1).astype(np.int64)
            ty = np.clip(np.floor((1 - t[:, 1] % 1.0) * th), 0, th - 1).astype(np.int64)
            rgba = self.tex[ty, tx].astype(np.float32) / 255
        else:
            rgba[:, :3] = UNTEXTURED
        rgba *= self.factor
        if self.vertex_rgba is not None:
            rgba *= (self.vertex_rgba[faces[idx]] * w[..., None]).sum(1)
        return rgba


def _unit_rgba(raw: Any) -> np.ndarray:
    """trimesh yields uint8 0-255 (or floats 0-1 for hand-built materials): normalise to float32 0-1."""
    arr = np.asarray(raw)
    out = arr.astype(np.float32) / (255 if arr.dtype.kind in "iu" else 1)
    out = np.concatenate([out.ravel()[:4], np.ones(max(0, 4 - out.size), np.float32)])
    return np.clip(out, 0, 1).astype(np.float32)


def _texture(mat: Any, uv: Any, n_vertices: int) -> tuple[np.ndarray | None, np.ndarray | None, bool]:
    img = getattr(mat, "baseColorTexture", None) or getattr(mat, "image", None)
    if img is None:
        return None, None, False
    if uv is None or len(uv) != n_vertices:
        return None, None, True
    return np.asarray(img.convert("RGBA")), np.asarray(uv, dtype=np.float32), True


def material_of(mesh: trimesh.Trimesh) -> MeshMaterial:
    vis, mat = mesh.visual, getattr(mesh.visual, "material", None)
    tex, uv, referenced = _texture(mat, getattr(vis, "uv", None), len(mesh.vertices))
    raw = getattr(mat, "baseColorFactor", None)
    raw = getattr(mat, "diffuse", None) if raw is None else raw
    factor = _unit_rgba(raw) if raw is not None else np.ones(4, np.float32)
    vertex = None
    if mat is None and getattr(vis, "kind", None) == "vertex":
        vertex = np.asarray(vis.vertex_colors, dtype=np.float32) / 255
    mode = str(getattr(mat, "alphaMode", None) or "OPAQUE").upper()
    cutoff = getattr(mat, "alphaCutoff", None)
    coloured = bool((factor[:3] < 0.999).any()) or (vertex is not None and bool((vertex[:, :3] < 0.999).any()))
    return MeshMaterial(tex, uv, factor, vertex, mode if mode in ("MASK", "BLEND") else "OPAQUE",
                        DEFAULT_CUTOFF if cutoff is None else float(cutoff), referenced, coloured)


def gltf_json(data: bytes) -> dict[str, Any]:
    try:
        jlen = struct.unpack_from("<I", data, 12)[0]
        doc = json.loads(data[20:20 + jlen])
    except (struct.error, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    try:
        check_document_shape(doc)
    except GlbRejected:
        return {}  # same as unreadable JSON: no extension/sampler facts
    return doc


def check_required(doc: dict[str, Any]) -> None:
    bad = sorted(set(doc.get("extensionsRequired") or []) - SUPPORTED_EXTENSIONS)
    if bad:
        raise RenderUnsupported(f"the source requires glTF extensions the renderer does not support: {', '.join(bad)}")


def gltf_warnings(doc: dict[str, Any], mats: list[MeshMaterial]) -> set[str]:
    out = {f"unsupported_extension:{e}" for e in doc.get("extensionsUsed") or [] if e not in SUPPORTED_EXTENSIONS}
    if any(s.get(k) in UNSUPPORTED_WRAP for s in doc.get("samplers") or [] for k in ("wrapS", "wrapT")):
        out.add("sampler_wrap_ignored")
    json_tex = any((m.get("pbrMetallicRoughness") or {}).get("baseColorTexture") for m in doc.get("materials") or [])
    if any(m.texture_referenced and m.tex is None for m in mats) or (
            json_tex and not any(m.tex is not None for m in mats)):
        out.add("texture_unrendered")
    if any(m.alpha_mode == "BLEND" for m in mats):
        out.add("blend_approximated")
    if not any(m.tex is not None or m.coloured for m in mats):
        out.add("texture_missing")
    return out
