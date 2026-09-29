"""Pure raster derivatives for image-kind builds (cut-out, canvas, size variants, tiling). CPU only.

Arrays are uint8: RGB (H, W, 3), RGBA (H, W, 4), masks (H, W).
"""
from __future__ import annotations

import io
from typing import Literal

import numpy as np
from PIL import Image
from scipy import ndimage

from .metrics import _srgb_to_lab

Pivot = Literal["center", "bottom_center"]


class RasterError(ValueError):
    pass


def to_png(arr: np.ndarray) -> bytes:
    out = io.BytesIO()
    Image.fromarray(arr).save(out, "PNG", optimize=True)
    return out.getvalue()


def decode_rgba(data: bytes, mode: str = "RGBA") -> np.ndarray:
    with Image.open(io.BytesIO(data)) as im:
        return np.asarray(im.convert(mode))


def apply_mask(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Soft alpha from the mask; hidden pixels take the nearest opaque colour so filtering/mips don't halo."""
    if rgb.shape[:2] != mask.shape:
        raise RasterError(f"mask {mask.shape[::-1]} does not match image {rgb.shape[1::-1]}")
    solid = mask >= 128
    if not solid.any():
        raise RasterError("mask is empty: no foreground to cut out")
    _, (iy, ix) = ndimage.distance_transform_edt(~solid, return_indices=True)
    bled = rgb[iy, ix]
    return np.dstack([bled, mask]).astype(np.uint8)


def trim(rgba: np.ndarray, threshold: int) -> tuple[np.ndarray, list[int]]:
    """Crop to the alpha >= threshold bounding box; returns the crop and its [x0, y0, x1, y1) box."""
    ys, xs = np.nonzero(rgba[..., 3] >= threshold)
    if len(xs) == 0:
        raise RasterError(f"no pixel reaches alpha {threshold}")
    box = [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]
    return rgba[box[1]:box[3], box[0]:box[2]], box


def resize_rgba(rgba: np.ndarray, width: int, height: int) -> np.ndarray:
    """Premultiplied Lanczos resize: unpremultiplied resampling pulls hidden colours into the edge."""
    im = Image.fromarray(rgba, "RGBA").convert("RGBa")
    return np.asarray(im.resize((width, height), Image.Resampling.LANCZOS).convert("RGBA"))


def fit_canvas(content: np.ndarray, canvas: int, padding: float, pivot: Pivot) -> tuple[np.ndarray, list[int]]:
    """Place trimmed content on a transparent canvas; canvas 0 = content size + padding. Returns (canvas, pivot xy)."""
    if not 0 <= padding < 0.5:
        raise RasterError("padding must be in [0, 0.5)")
    h, w = content.shape[:2]
    if canvas <= 0:
        pad = round(max(w, h) * padding)
        cw, ch, scaled = w + 2 * pad, h + 2 * pad, content
    else:
        pad = round(canvas * padding)
        s = max(1, canvas - 2 * pad) / max(w, h)
        nw, nh = max(1, round(w * s)), max(1, round(h * s))
        scaled = resize_rgba(content, nw, nh) if (nw, nh) != (w, h) else content
        cw = ch = canvas
    sh, sw = scaled.shape[:2]
    x = (cw - sw) // 2
    y = (ch - sh) // 2 if pivot == "center" else ch - sh - pad
    out = np.zeros((ch, cw, 4), dtype=np.uint8)
    out[y:y + sh, x:x + sw] = scaled
    piv = [cw // 2, ch // 2] if pivot == "center" else [cw // 2, y + sh]
    return out, piv


def square_variants(rgba: np.ndarray, sizes: list[int]) -> dict[int, np.ndarray]:
    h, w = rgba.shape[:2]
    if h != w:
        side = max(h, w)
        sq = np.zeros((side, side, 4), dtype=np.uint8)
        sq[(side - h) // 2:(side - h) // 2 + h, (side - w) // 2:(side - w) // 2 + w] = rgba
        rgba = sq
    return {s: resize_rgba(rgba, s, s) for s in sorted(set(sizes), reverse=True)}


def seam_stats(rgb: np.ndarray) -> dict[str, float]:
    """Mean ΔE76 across the wrap-around edges vs. between ordinary neighbours; ratio ≈ 1 means seamless."""
    lab = _srgb_to_lab(rgb)
    interior = float(np.mean([np.linalg.norm(np.diff(lab, axis=a), axis=-1).mean() for a in (0, 1)]))
    wrap = float(np.mean([np.linalg.norm(lab[:, -1] - lab[:, 0], axis=-1).mean(),
                          np.linalg.norm(lab[-1] - lab[0], axis=-1).mean()]))
    return {"wrap_delta": round(wrap, 3), "interior_delta": round(interior, 3),
            "ratio": round(wrap / max(interior, 1e-6), 3)}


def tile_preview(rgb: np.ndarray, n: int = 3, max_px: int = 1024) -> np.ndarray:
    tiled = np.tile(rgb, (n, n, 1))
    im = Image.fromarray(tiled)
    im.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    return np.asarray(im)
