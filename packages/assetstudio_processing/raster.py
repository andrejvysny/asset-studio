"""Pure raster derivatives for image-kind builds (cut-out, canvas, size variants, tiling). CPU only.

Arrays are uint8: RGB (H, W, 3), RGBA (H, W, 4), masks (H, W).
"""
from __future__ import annotations

import io
from typing import Any, Literal

import numpy as np
from PIL import Image, ImageOps
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


def resize_keep_aspect(rgba: np.ndarray, max_w: int, max_h: int, resample: str) -> np.ndarray:
    """Fit inside the box, no crop or distortion; may upscale. Lanczos is premultiplied (no dark halos)."""
    h, w = rgba.shape[:2]
    s = min(max_w / w, max_h / h)
    nw, nh = min(max_w, max(1, round(w * s))), min(max_h, max(1, round(h * s)))
    if (nw, nh) == (w, h):
        return rgba
    if resample == "nearest":
        return np.asarray(Image.fromarray(rgba, "RGBA").resize((nw, nh), Image.Resampling.NEAREST))
    if resample != "lanczos":
        raise RasterError(f"unknown resample {resample}")
    return resize_rgba(rgba, nw, nh)


def _background(background: str) -> tuple[int, int, int, int] | None:
    if background in ("transparent", "source_edge"):
        return None
    try:
        r, g, b = (int(background[i:i + 2], 16) for i in (1, 3, 5))
    except ValueError as e:
        raise RasterError("background must be transparent, source_edge or #rrggbb") from e
    return r, g, b, 255


def pad_canvas(rgba: np.ndarray, width: int, height: int, placement: str,
               background: str) -> tuple[np.ndarray, dict[str, Any]]:
    """Explicit canvas, content never scaled or cropped. Returns (canvas, {content_bounds [x,y,w,h], pivot [x,y]})."""
    h, w = rgba.shape[:2]
    if w > width or h > height:
        raise RasterError("content larger than canvas; resize first")
    x = 0 if placement == "top_left" else (width - w) // 2
    y = height - h if placement == "bottom_center" else 0 if placement == "top_left" else (height - h) // 2
    if background == "source_edge":
        ys = np.clip(np.arange(height) - y, 0, h - 1)
        xs = np.clip(np.arange(width) - x, 0, w - 1)
        out = rgba[ys[:, None], xs[None, :]].copy()
    else:
        out = np.empty((height, width, 4), dtype=np.uint8)
        out[:] = _background(background) or (0, 0, 0, 0)
    out[y:y + h, x:x + w] = rgba
    pivot = [x + w // 2, y + h] if placement == "bottom_center" else [x + w // 2, y + h // 2]
    return out, {"content_bounds": [x, y, w, h], "pivot": pivot}


def _decode_oriented(data: bytes) -> np.ndarray:
    from .images import inspect_image

    try:
        inspect_image(data, ("PNG", "JPEG", "WEBP"))  # pixel cap + verified decode before any transform
    except ValueError as e:
        raise RasterError(str(e)) from e
    with Image.open(io.BytesIO(data)) as im:
        return np.asarray(ImageOps.exif_transpose(im).convert("RGBA"))


def apply_raster_transform(data: bytes, transform: Any) -> tuple[bytes, dict[str, Any]]:
    """Decode (EXIF orientation applied to the derivative only), resize or pad, encode PNG. Input bytes untouched."""
    from assetstudio_core.variants import PadCanvas, RasterTransform, ResizeKeepAspect
    from pydantic import TypeAdapter, ValidationError

    if not isinstance(transform, (ResizeKeepAspect, PadCanvas)):
        try:
            transform = TypeAdapter(RasterTransform).validate_python(transform)
        except ValidationError as e:
            raise RasterError(f"invalid raster transform: {e}") from e
    src = _decode_oriented(data)
    h, w = src.shape[:2]
    meta: dict[str, Any] = {"op": transform.op, "input_size": [w, h], "resample": None, "background": None}
    if isinstance(transform, ResizeKeepAspect):
        out = resize_keep_aspect(src, transform.max_width, transform.max_height, transform.resample)
        meta.update(resample=transform.resample, content_bounds=[0, 0, out.shape[1], out.shape[0]])
    else:
        out, info = pad_canvas(src, transform.width, transform.height, transform.placement, transform.background)
        meta.update(background=transform.background, **info)
    meta["output_size"] = [out.shape[1], out.shape[0]]
    return to_png(out), meta
