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
