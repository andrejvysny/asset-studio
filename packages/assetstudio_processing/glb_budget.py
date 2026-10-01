"""Cheap, CPU-only GLB budget measurements against the iPad limits in contracts capabilities.json.

Informational (stored beside a delivery, never part of contract bytes): clients decide what to do with it.
"""
from __future__ import annotations

import json
import struct
from typing import Any

from .glb import GlbRejected, inspect_container

TRIANGLES_MODE = 4


def glb_json(data: bytes) -> tuple[dict[str, Any], memoryview]:
    """(glTF JSON document, BIN chunk payload) of a structurally valid GLB. Raises GlbRejected."""
    inspect_container(data)
    json_len = struct.unpack_from("<I", data, 12)[0]
    doc = json.loads(data[20:20 + json_len])
    end, blob = 20 + json_len, memoryview(b"")
    if end + 8 <= len(data):
        clen, ctype = struct.unpack_from("<I4s", data, end)
        if ctype == b"BIN\x00" and end + 8 + clen <= len(data):
            blob = memoryview(data)[end + 8:end + 8 + clen]
    return doc, blob


def image_size(head: bytes) -> tuple[int, int] | None:
    """(width, height) from PNG / JPEG / WebP headers; None when the format or header is not recognised."""
    if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
        return struct.unpack(">II", head[16:24])
    if head[:2] == b"\xff\xd8":
        return _jpeg_size(head)
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return _webp_size(head)
    return None


def _jpeg_size(b: bytes) -> tuple[int, int] | None:
    i = 2
    while i + 9 < len(b):
        if b[i] != 0xFF:
            i += 1
            continue
        marker = b[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            i += 1 if marker == 0xFF else 2
            continue
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", b[i + 5:i + 9])
            return w, h
        i += 2 + struct.unpack(">H", b[i + 2:i + 4])[0]
    return None


def _webp_size(b: bytes) -> tuple[int, int] | None:
    kind = b[12:16]
    if kind == b"VP8X" and len(b) >= 30:
        return (int.from_bytes(b[24:27], "little") + 1, int.from_bytes(b[27:30], "little") + 1)
    if kind == b"VP8L" and len(b) >= 25 and b[20] == 0x2F:
        bits = int.from_bytes(b[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8 " and len(b) >= 30 and b[23:26] == b"\x9d\x01\x2a":
        w, h = struct.unpack("<HH", b[26:30])
        return w & 0x3FFF, h & 0x3FFF
    return None


def _accessor_count(doc: dict[str, Any], index: Any) -> int:
    try:
        return int(doc["accessors"][index]["count"])
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


def count_triangles(doc: dict[str, Any]) -> int:
    total = 0
    for mesh in doc.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            if prim.get("mode", TRIANGLES_MODE) != TRIANGLES_MODE:
                continue
            idx = prim.get("indices")
            src = idx if idx is not None else (prim.get("attributes") or {}).get("POSITION")
            total += _accessor_count(doc, src) // 3
    return total


def _max_texture_px(doc: dict[str, Any], blob: memoryview, warnings: list[str]) -> int | None:
    best: int | None = None
    for i, img in enumerate(doc.get("images") or []):
        size = None
        try:
            bv = doc["bufferViews"][img["bufferView"]]
            off = bv.get("byteOffset", 0)
            size = image_size(bytes(blob[off:off + min(bv["byteLength"], 65536)]))
        except (KeyError, IndexError, TypeError):
            pass
        if size is None:
            warnings.append(f"image {i}: dimensions unknown")
            continue
        best = max(best or 0, size[0], size[1])
    return best


def glb_budget(data: bytes, limits: dict[str, Any]) -> dict[str, Any]:
    """Counts + iPad budget verdict. `limits` is capabilities.json["limits"] (ipad_* keys)."""
    try:
        doc, blob = glb_json(data)
    except (GlbRejected, ValueError) as e:
        return {"within_ipad_budget": False, "exceeded": ["unreadable"], "warnings": [str(e)[:200]],
                "glb_bytes": len(data), "triangles": None, "nodes": None, "materials": None, "max_texture_px": None}
    warnings: list[str] = []
    stats: dict[str, Any] = {
        "glb_bytes": len(data), "triangles": count_triangles(doc), "nodes": len(doc.get("nodes") or []),
        "materials": len(doc.get("materials") or []), "max_texture_px": _max_texture_px(doc, blob, warnings)}
    checks = {"glb_bytes": "ipad_glb_max_bytes", "triangles": "ipad_max_triangles", "nodes": "ipad_max_nodes",
              "materials": "ipad_max_materials", "max_texture_px": "ipad_max_texture_px"}
    exceeded = [k for k, lim in checks.items()
                if stats[k] is not None and lim in limits and stats[k] > limits[lim]]
    return {**stats, "within_ipad_budget": not exceeded, "exceeded": exceeded, "warnings": warnings}
