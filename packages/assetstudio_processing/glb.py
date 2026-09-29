"""Self-contained GLB inspection: container structure first (no external URIs), then mesh reload checks."""
from __future__ import annotations

import json
import struct
import tempfile
from pathlib import Path
from typing import Any

MAX_GLB_BYTES = 512 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024


class GlbRejected(ValueError):
    pass


def inspect_container(data: bytes) -> dict[str, Any]:
    """Validate the GLB binary layout and reject any external or non-data URI reference."""
    if len(data) > MAX_GLB_BYTES:
        raise GlbRejected(f"GLB larger than {MAX_GLB_BYTES} bytes")
    if len(data) < 20:
        raise GlbRejected("file too small to be a GLB")
    magic, version, length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF" or version != 2:
        raise GlbRejected("not a glTF 2.0 binary (GLB)")
    if length != len(data):
        raise GlbRejected(f"declared length {length} != file size {len(data)}")
    json_len, json_type = struct.unpack_from("<I4s", data, 12)
    if json_type != b"JSON" or json_len > MAX_JSON_BYTES or 20 + json_len > len(data):
        raise GlbRejected("missing or oversized JSON chunk")
    try:
        doc = json.loads(data[20:20 + json_len])
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise GlbRejected(f"invalid JSON chunk: {e}") from e
    if not isinstance(doc, dict):
        raise GlbRejected("JSON chunk is not an object")
    for section in ("buffers", "images"):
        for i, entry in enumerate(doc.get(section, []) or []):
            uri = entry.get("uri") if isinstance(entry, dict) else None
            if uri is not None:
                raise GlbRejected(f"{section}[{i}] has a URI reference; only self-contained GLB is accepted")
    return {
        "generator": (doc.get("asset") or {}).get("generator"),
        "meshes": len(doc.get("meshes", []) or []),
        "materials": len(doc.get("materials", []) or []),
        "images": len(doc.get("images", []) or []),
        "extensions_required": doc.get("extensionsRequired", []),
    }


TEXTURE_CHECKS = ("uvs_present", "material_texture_present")


def validate_glb_bytes(data: bytes, require_texture: bool = False) -> dict[str, Any]:
    """Container check + trimesh reload (mesh.validate_glb). Structural validation, never artistic QA.

    UV/texture checks are reported always but only required when the output contract needs a textured mesh.
    """
    from .mesh import validate_glb

    try:
        container = inspect_container(data)
    except GlbRejected as e:
        return {"ok": False, "checks": [{"id": "container", "ok": False, "detail": str(e)}], "container": None}
    with tempfile.NamedTemporaryFile(suffix=".glb") as f:
        f.write(data)
        f.flush()
        result = validate_glb(Path(f.name))
    result["checks"].insert(0, {"id": "container", "ok": True, "detail": "self-contained GLB 2.0"})
    for c in result["checks"]:
        c["required"] = require_texture or c["id"] not in TEXTURE_CHECKS
    required = [c for c in result["checks"] if c["required"]]
    reload_ok = any(c["id"] == "reload" and c["ok"] for c in result["checks"])
    result["ok"] = reload_ok and all(c["ok"] for c in required)
    result["container"] = container
    return result
