"""Frame sequences → atlas. Untrusted input: bounded archives, PNG-only frames, uniform frame size. CPU only."""
from __future__ import annotations

import io
import math
import re
import stat
import zipfile
from pathlib import PurePosixPath
from typing import Any

import numpy as np

from .images import ImageRejected, inspect_image
from .raster import decode_rgba

MAX_FRAMES = 1024
MAX_ARCHIVE_BYTES = 512 * 2**20  # total uncompressed
MAX_ATLAS_PX = 8192
IGNORED = ("__MACOSX/", ".DS_Store")


class FrameError(ValueError):
    pass


def natural_key(name: str) -> list[Any]:
    """frame_2 sorts before frame_10."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _safe_member(info: zipfile.ZipInfo) -> bool:
    p = PurePosixPath(info.filename)
    if info.filename.startswith("/") or "\\" in info.filename or ".." in p.parts:
        raise FrameError(f"unsafe path in archive: {info.filename!r}")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise FrameError(f"symlink in archive: {info.filename!r}")
    return not info.is_dir() and not any(s in info.filename for s in IGNORED)


def read_zip(data: bytes) -> list[tuple[str, bytes]]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise FrameError(f"not a zip archive: {e}") from e
    with zf:
        members = [i for i in zf.infolist() if _safe_member(i)]
        if len(members) > MAX_FRAMES:
            raise FrameError(f"{len(members)} entries; limit is {MAX_FRAMES}")
        if sum(i.file_size for i in members) > MAX_ARCHIVE_BYTES:
            raise FrameError(f"archive expands beyond {MAX_ARCHIVE_BYTES // 2**20} MiB")
        out = []
        for i in members:
            with zf.open(i) as f:
                body = f.read(i.file_size + 1)  # declared sizes can lie; never read past them
            if len(body) > i.file_size:
                raise FrameError(f"{i.filename}: size mismatch")
            out.append((i.filename, body))
        return out


def decode_frames(files: list[tuple[str, bytes]]) -> tuple[list[str], list[np.ndarray]]:
    """Natural-sorted PNG frames of one size, as RGBA (H, W, 4)."""
    if not files:
        raise FrameError("no frames")
    if len(files) > MAX_FRAMES:
        raise FrameError(f"{len(files)} frames; limit is {MAX_FRAMES}")
    files = sorted(files, key=lambda f: natural_key(f[0]))
    if len({n for n, _ in files}) != len(files):
        raise FrameError("duplicate frame names")
    sizes: dict[tuple[int, int], list[str]] = {}
    for name, data in files:
        try:
            info = inspect_image(data, ("PNG",))
        except ImageRejected as e:
            raise FrameError(f"{name}: {e}") from e
        sizes.setdefault((info.width, info.height), []).append(name)
    if len(sizes) > 1:
        detail = "; ".join(f"{w}x{h}: {', '.join(n[:3])}" for (w, h), n in sizes.items())
        raise FrameError(f"frames differ in size ({detail})")
    return [n for n, _ in files], [decode_rgba(d) for _, d in files]


def pack_grid(frames: list[np.ndarray], cols: int = 0, padding: int = 0,
              pow2: bool = False) -> tuple[np.ndarray, list[list[int]], int, int]:
    """Row-major grid; rects are [x, y, w, h]. cols 0 = near-square grid."""
    n = len(frames)
    h, w = frames[0].shape[:2]
    cols = cols or math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    aw, ah = cols * (w + padding) + padding, rows * (h + padding) + padding
    if pow2:
        aw, ah = 1 << (aw - 1).bit_length(), 1 << (ah - 1).bit_length()
    if max(aw, ah) > MAX_ATLAS_PX:
        raise FrameError(f"atlas would be {aw}x{ah}; limit is {MAX_ATLAS_PX}px per side")
    atlas = np.zeros((ah, aw, 4), dtype=np.uint8)
    rects = []
    for i, f in enumerate(frames):
        x, y = padding + (i % cols) * (w + padding), padding + (i // cols) * (h + padding)
        atlas[y:y + h, x:x + w] = f
        rects.append([x, y, w, h])
    return atlas, rects, cols, rows


def atlas_meta(names: list[str], rects: list[list[int]], atlas_size: tuple[int, int], **fields: Any) -> dict[str, Any]:
    return {"format": "assetstudio.atlas/1", "size": list(atlas_size),
            "frames": [{"index": i, "source": n, "rect": r} for i, (n, r) in enumerate(zip(names, rects, strict=True))],
            **fields}
