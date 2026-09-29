"""Safe image inspection and deterministic derivatives. Untrusted input: bounded pixels, verified decode."""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

MAX_PIXELS = 64_000_000
FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
THUMB_PX = 384


class ImageRejected(ValueError):
    pass


@dataclass(frozen=True)
class ImageInfo:
    format: str
    mime: str
    width: int
    height: int
    mode: str
    has_alpha: bool

    def as_meta(self) -> dict:
        return {"format": self.format, "width": self.width, "height": self.height, "mode": self.mode,
                "has_alpha": self.has_alpha}


def inspect_image(data: bytes, allowed: tuple[str, ...] = ("PNG", "JPEG")) -> ImageInfo:
    """Decode fully (catches truncated/corrupt files) with a pixel cap checked before decoding."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = im.format or ""
            if fmt not in allowed:
                raise ImageRejected(f"unsupported image format {fmt or 'unknown'}; allowed {', '.join(allowed)}")
            w, h = im.size
            if w <= 0 or h <= 0 or w * h > MAX_PIXELS:
                raise ImageRejected(f"image is {w}x{h}; limit is {MAX_PIXELS} pixels")
            im.load()
            has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
            return ImageInfo(fmt, FORMATS[fmt], w, h, im.mode, has_alpha)
    except ImageRejected:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError) as e:
        raise ImageRejected(f"not a decodable image: {e}") from e


def thumbnail_png(data: bytes, size: int = THUMB_PX) -> bytes:
    """Aspect-preserving preview; alpha kept so the UI can show a checkerboard."""
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P", "PA") else im.convert("RGB")
        im.thumbnail((size, size), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        im.save(out, "PNG", optimize=True)
        return out.getvalue()


def to_png(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        out = io.BytesIO()
        im.save(out, "PNG")
        return out.getvalue()


def load_rgb_array(data: bytes):  # -> np.ndarray (H, W, 3) uint8
    import numpy as np

    with Image.open(io.BytesIO(data)) as im:
        return np.asarray(im.convert("RGB"))


def dhash64(data: bytes) -> int:
    """64-bit difference hash (9x8 grayscale, left<right per row): cheap perceptual fingerprint, alpha flattened
    onto white so a transparent cutout and its opaque twin compare equal."""
    with Image.open(io.BytesIO(data)) as im:
        rgba = im.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
    px = list(flat.convert("L").resize((9, 8), Image.Resampling.LANCZOS).tobytes())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | int(px[row * 9 + col] < px[row * 9 + col + 1])
    return bits


def hamming64(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def crop_png(data: bytes, crop: dict | list | tuple | None) -> bytes:
    """Crop to {x, y, w, h}: fractions of the image when every value is <= 1, else pixels. None/invalid -> the
    original bytes (a malformed crop must not make the reference unusable)."""
    if not crop:
        return data
    try:
        x, y, w, h = ([crop[k] for k in "xywh"] if isinstance(crop, dict) else list(crop))[:4]
        x, y, w, h = float(x), float(y), float(w), float(h)
    except (KeyError, TypeError, ValueError):
        return data
    with Image.open(io.BytesIO(data)) as im:
        iw, ih = im.size
        s = (iw, ih, iw, ih) if max(x, y, w, h) <= 1.0 else (1, 1, 1, 1)
        box = (int(x * s[0]), int(y * s[1]), int((x + w) * s[2]), int((y + h) * s[3]))
        box = (max(0, box[0]), max(0, box[1]), min(iw, box[2]), min(ih, box[3]))
        if box[2] - box[0] < 1 or box[3] - box[1] < 1:
            return data
        out = io.BytesIO()
        im.crop(box).save(out, "PNG")
        return out.getvalue()
