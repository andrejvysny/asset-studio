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


# glTF 2.0 shapes of every field our readers touch (glb, transforms, render_materials). A wrong shape is a
# controlled rejection here, instead of an AttributeError/TypeError deep inside a reader.
_ARRAYS = ("accessors", "animations", "buffers", "bufferViews", "cameras", "images", "materials", "meshes", "nodes",
           "samplers", "scenes", "skins", "textures")
_INT, _NUM, _STR, _OBJ, _INTS, _NUMS, _LIST = "int", "number", "string", "object", "int[]", "number[]", "array"
_FIELDS: dict[str, dict[str, str]] = {
    "accessors": {"bufferView": _INT, "byteOffset": _INT, "count": _INT, "componentType": _INT, "type": _STR},
    "bufferViews": {"buffer": _INT, "byteOffset": _INT, "byteLength": _INT, "byteStride": _INT},
    "images": {"bufferView": _INT},
    "materials": {"pbrMetallicRoughness": _OBJ, "alphaMode": _STR, "alphaCutoff": _NUM},
    "meshes": {"primitives": _LIST},
    "nodes": {"children": _INTS, "mesh": _INT, "skin": _INT, "matrix": _NUMS, "translation": _NUMS,
              "rotation": _NUMS, "scale": _NUMS, "weights": _NUMS},
    "samplers": {"wrapS": _INT, "wrapT": _INT},
    "scenes": {"nodes": _INTS},
}
_PRIMITIVE = {"attributes": _OBJ, "indices": _INT, "material": _INT, "mode": _INT, "targets": _LIST}


def _is(value: Any, shape: str) -> bool:
    number = isinstance(value, (int, float)) and not isinstance(value, bool)
    return {_INT: isinstance(value, int) and not isinstance(value, bool), _NUM: number, _STR: isinstance(value, str),
            _OBJ: isinstance(value, dict), _LIST: isinstance(value, list),
            _INTS: isinstance(value, list) and all(_is(v, _INT) for v in value),
            _NUMS: isinstance(value, list) and all(_is(v, _NUM) for v in value)}[shape]


def _check_fields(obj: dict[str, Any], fields: dict[str, str], where: str) -> None:
    for key, shape in fields.items():
        if key in obj and not _is(obj[key], shape):
            raise GlbRejected(f"{where}.{key} must be {'an' if shape[0] in 'aio' else 'a'} {shape}")


def check_document_shape(doc: dict[str, Any]) -> None:
    """Raise GlbRejected (with the offending JSON path) when a glTF field has the wrong JSON type."""
    if "asset" in doc and not isinstance(doc["asset"], dict):
        raise GlbRejected("asset must be an object")
    if "scene" in doc and not _is(doc["scene"], _INT):
        raise GlbRejected("scene must be an int")
    for key in ("extensionsRequired", "extensionsUsed"):
        if key in doc and not (isinstance(doc[key], list) and all(isinstance(v, str) for v in doc[key])):
            raise GlbRejected(f"{key} must be an array of strings")
    for section in _ARRAYS:
        entries = doc.get(section)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise GlbRejected(f"{section} must be an array")
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise GlbRejected(f"{section}[{i}] must be an object")
            _check_fields(entry, _FIELDS.get(section, {}), f"{section}[{i}]")
    for mi, mesh in enumerate(doc.get("meshes") or []):
        for pi, prim in enumerate(mesh.get("primitives") or []):
            if not isinstance(prim, dict):
                raise GlbRejected(f"meshes[{mi}].primitives[{pi}] must be an object")
            _check_fields(prim, _PRIMITIVE, f"meshes[{mi}].primitives[{pi}]")


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
    check_document_shape(doc)
    for section in ("buffers", "images"):
        for i, entry in enumerate(doc.get(section) or []):
            if entry.get("uri") is not None:
                raise GlbRejected(f"{section}[{i}] has a URI reference; only self-contained GLB is accepted")
    return {
        "generator": (doc.get("asset") or {}).get("generator"),
        "meshes": len(doc.get("meshes") or []),
        "materials": len(doc.get("materials") or []),
        "images": len(doc.get("images") or []),
        "extensions_required": doc.get("extensionsRequired") or [],
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
