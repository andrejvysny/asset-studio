"""Binary member checks for source packages: image headers and static self-contained GLBs."""
from __future__ import annotations

import struct
from pathlib import Path

from .glb import MAX_GLB_BYTES, GlbRejected, inspect_container
from .source_report import SourcePackageError
from .transforms import TransformRejected, inspect_static_glb

MAX_SIDE = 16384
MAX_PIXEL_BYTES = 256 * 1024 * 1024
HEADER_READ = 16 * 1024 * 1024
IMAGE_KINDS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}
_PNG = b"\x89PNG\r\n\x1a\n"
_NO_LENGTH = frozenset({0x01, *range(0xD0, 0xDA)})


def _bad(detail: str, message: str, path: str, code: str = "unsafe_package") -> SourcePackageError:
    return SourcePackageError(code, detail, message, path)


def _png_size(head: bytes) -> tuple[int, int] | None:
    if head[:8] != _PNG:
        return None
    if head[12:16] != b"IHDR" or len(head) < 24:
        raise ValueError("missing IHDR")
    return struct.unpack(">II", head[16:24])


def _jpeg_size(head: bytes) -> tuple[int, int] | None:
    if head[:3] != b"\xff\xd8\xff":
        return None
    i = 2
    while i + 4 <= len(head):
        if head[i] != 0xFF:
            raise ValueError("bad marker")
        marker = head[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in _NO_LENGTH or marker == 0xD8:
            i += 2
            continue
        length = struct.unpack(">H", head[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 9 > len(head):
                break
            h, w = struct.unpack(">HH", head[i + 5:i + 9])
            return w, h
        i += 2 + length
    raise ValueError("no frame header")


def _webp_size(head: bytes) -> tuple[int, int] | None:
    if head[:4] != b"RIFF" or head[8:12] != b"WEBP":
        return None
    kind = head[12:16]
    if kind == b"VP8 " and len(head) >= 30 and head[23:26] == b"\x9d\x01\x2a":
        w, h = struct.unpack("<HH", head[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if kind == b"VP8L" and len(head) >= 25 and head[20] == 0x2F:
        bits = int.from_bytes(head[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8X" and len(head) >= 30:
        return int.from_bytes(head[24:27], "little") + 1, int.from_bytes(head[27:30], "little") + 1
    raise ValueError("unsupported WebP layout")


_READERS = {"png": _png_size, "jpeg": _jpeg_size, "webp": _webp_size}


def check_image(path: str, file: Path, suffix: str) -> None:
    kind = IMAGE_KINDS[suffix]
    with file.open("rb") as f:
        head = f.read(HEADER_READ)
    try:
        size = _READERS[kind](head)
    except (ValueError, struct.error) as e:
        raise _bad("image_invalid", f"unreadable {kind} header: {e}", path) from e
    if size is None:
        raise _bad("image_magic_mismatch", f"content is not {kind} data", path)
    width, height = size
    if width < 1 or height < 1:
        raise _bad("image_invalid", "image has zero size", path)
    if width > MAX_SIDE or height > MAX_SIDE or width * height * 4 > MAX_PIXEL_BYTES:
        raise _bad("image_dimensions", f"image {width}x{height} exceeds the decode budget", path, "resource_limit")


def check_glb(path: str, file: Path) -> None:
    if file.stat().st_size > MAX_GLB_BYTES:
        raise _bad("glb_size", f"GLB larger than {MAX_GLB_BYTES} bytes", path, "resource_limit")
    data = file.read_bytes()
    try:
        inspect_container(data)
        inspect_static_glb(data)
    except GlbRejected as e:
        raise _bad("glb_external_uri" if "URI" in str(e) else "glb_invalid", str(e), path) from e
    except TransformRejected as e:
        if e.code == "corrupt_source":
            raise _bad("glb_invalid", str(e), path) from e
        raise _bad("glb_external_uri" if "URI" in str(e) else "glb_not_static", str(e), path) from e
