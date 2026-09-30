"""Deterministic material policy on a self-contained GLB: alpha mode/cutoff, culling, metallic replace and roughness
clamp, applied to every material (a whole-asset build profile).

Why texture rewrites: glTF metallic/roughness = factor x texture channel, and factors are limited to 0..1. A factor can
only scale values down; it cannot raise roughness or clamp it into a range. `roughness_min/max` and `metallic` are
therefore applied to the packed metallicRoughness texture (G = roughness, B = metallic; linear data, never
colour-managed), with the factor reset to 1. Geometry, accessors and every other buffer view keep their bytes;
`preservation_checks` proves that independently of this code path.
"""
from __future__ import annotations

import io
import json
import struct
from typing import Any

import numpy as np
from PIL import Image

from .glb import GlbRejected, inspect_container

AUTO_MASK_FRACTION = 0.01  # `auto`: MASK when more than 1% of base-colour texels are below the cutoff
DEFAULT_CUTOFF = 0.5
ALPHA_MODES = {"opaque": "OPAQUE", "mask": "MASK", "blend": "BLEND"}
MATERIAL_KEYS = ("alphaMode", "alphaCutoff", "doubleSided", "pbrMetallicRoughness")


class MaterialRejected(ValueError):
    """`code`: unsupported_source_features | corrupt_source."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def read_glb(data: bytes) -> tuple[dict[str, Any], bytes]:
    """(JSON doc, BIN payload). Only self-contained GLBs with at most one buffer are accepted."""
    try:
        inspect_container(data)
    except GlbRejected as e:
        raise MaterialRejected("corrupt_source", str(e)) from e
    json_len = struct.unpack_from("<I", data, 12)[0]
    doc = json.loads(data[20:20 + json_len])
    end, payload = 20 + json_len, b""
    if end + 8 <= len(data):
        clen, ctype = struct.unpack_from("<I4s", data, end)
        if ctype == b"BIN\x00" and end + 8 + clen <= len(data):
            payload = data[end + 8:end + 8 + clen]
    if len(doc.get("buffers") or []) > 1:
        raise MaterialRejected("unsupported_source_features", "more than one buffer")
    return doc, payload


def write_glb(doc: dict[str, Any], payload: bytes) -> bytes:
    body = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    body += b" " * (-len(body) % 4)
    chunks = struct.pack("<I4s", len(body), b"JSON") + body
    if payload:
        payload += b"\x00" * (-len(payload) % 4)
        chunks += struct.pack("<I4s", len(payload), b"BIN\x00") + payload
    return struct.pack("<4sII", b"glTF", 2, 12 + len(chunks)) + chunks


def _view_bytes(doc: dict[str, Any], payload: bytes, view: int) -> bytes:
    bv = doc["bufferViews"][view]
    start = int(bv.get("byteOffset", 0))
    return payload[start:start + int(bv["byteLength"])]


def _repack(doc: dict[str, Any], payload: bytes, replaced: dict[int, bytes]) -> bytes:
    """New BIN with `replaced` view payloads; every view keeps its index, offsets are re-laid out 4-byte aligned
    in original order. Overlapping or strided image views are refused rather than guessed."""
    views = doc.get("bufferViews") or []
    order = sorted(range(len(views)), key=lambda i: int(views[i].get("byteOffset", 0)))
    last_end = 0
    for i in order:
        start = int(views[i].get("byteOffset", 0))
        if start < last_end:
            raise MaterialRejected("unsupported_source_features", "overlapping buffer views")
        last_end = start + int(views[i]["byteLength"])
    out = bytearray()
    for i in order:
        data = replaced.get(i, _view_bytes(doc, payload, i))
        out += b"\x00" * (-len(out) % 4)
        views[i]["byteOffset"] = len(out)
        views[i]["byteLength"] = len(data)
        out += data
    if doc.get("buffers"):
        doc["buffers"][0]["byteLength"] = len(out) + (-len(out) % 4)
    return bytes(out)


def _texture_image(doc: dict[str, Any], texinfo: dict[str, Any] | None) -> int | None:
    if not texinfo:
        return None
    tex = (doc.get("textures") or [])[texinfo["index"]]
    return tex.get("source")


def _image_users(doc: dict[str, Any], image: int) -> int:
    """How many texture slots across all materials sample this image (a shared image cannot be rewritten)."""
    n = 0
    for m in doc.get("materials") or []:
        slots = [m.get("normalTexture"), m.get("occlusionTexture"), m.get("emissiveTexture")]
        pbr = m.get("pbrMetallicRoughness") or {}
        slots += [pbr.get("baseColorTexture"), pbr.get("metallicRoughnessTexture")]
        n += sum(1 for s in slots if s and _texture_image(doc, s) == image)
    return n


def _decode(doc: dict[str, Any], payload: bytes, image: int) -> np.ndarray:
    img = doc["images"][image]
    if "bufferView" not in img:
        raise MaterialRejected("unsupported_source_features", f"image {image} has no bufferView")
    with Image.open(io.BytesIO(_view_bytes(doc, payload, img["bufferView"]))) as im:
        im.load()
        return np.asarray(im.convert("RGBA"))


def _encode(rgba: np.ndarray) -> bytes:
    out = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(out, "PNG", optimize=False)
    return out.getvalue()


def _alpha(doc: dict[str, Any], payload: bytes, mat: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    mode = policy.get("alpha_mode")
    cutoff = float(policy["alpha_cutoff"]) if policy.get("alpha_cutoff") is not None else DEFAULT_CUTOFF
    rep: dict[str, Any] = {}
    if mode == "auto":
        pbr = mat.get("pbrMetallicRoughness") or {}
        factor = float((pbr.get("baseColorFactor") or [1, 1, 1, 1])[3])
        image = _texture_image(doc, pbr.get("baseColorTexture"))
        alpha = (_decode(doc, payload, image)[..., 3] / 255.0 * factor) if image is not None else np.full(1, factor)
        fraction = float((alpha < cutoff).mean())
        mode = "mask" if fraction > AUTO_MASK_FRACTION else "opaque"
        rep["auto"] = {"transparent_fraction": round(fraction, 6), "threshold": AUTO_MASK_FRACTION, "chosen": mode}
    if mode is None:
        return rep
    mat["alphaMode"] = ALPHA_MODES[mode]
    if mode == "mask":
        mat["alphaCutoff"] = cutoff
    else:
        mat.pop("alphaCutoff", None)
    return {**rep, "alpha_mode": mat["alphaMode"], "alpha_cutoff": mat.get("alphaCutoff")}


def _stats(channel: np.ndarray) -> dict[str, float]:
    q = np.quantile(channel, [0.0, 0.05, 0.5, 0.95, 1.0])
    return dict(zip(("min", "p05", "median", "p95", "max"), (round(float(v), 4) for v in q), strict=True))


def _metal_rough(doc: dict[str, Any], payload: bytes, mat: dict[str, Any], policy: dict[str, Any],
                 replaced: dict[int, bytes]) -> dict[str, Any]:
    lo, hi, metal = policy.get("roughness_min"), policy.get("roughness_max"), policy.get("metallic")
    if lo is None and hi is None and metal is None:
        return {}
    pbr = mat.setdefault("pbrMetallicRoughness", {})
    rf, mf = float(pbr.get("roughnessFactor", 1.0)), float(pbr.get("metallicFactor", 1.0))
    image = _texture_image(doc, pbr.get("metallicRoughnessTexture"))
    lo_v, hi_v = (0.0 if lo is None else float(lo)), (1.0 if hi is None else float(hi))
    if image is None:  # factor-only material: the factors are the values
        if lo is not None or hi is not None:
            pbr["roughnessFactor"] = float(np.clip(rf, lo_v, hi_v))
        if metal is not None:
            pbr["metallicFactor"] = float(metal)
        return {"roughness_factor": pbr.get("roughnessFactor", rf), "metallic_factor": pbr.get("metallicFactor", mf)}
    if _image_users(doc, image) > 1:
        raise MaterialRejected("unsupported_source_features", f"metallicRoughness image {image} is shared")
    rgba = _decode(doc, payload, image).copy()
    rough = rgba[..., 1] / 255.0 * rf  # effective linear roughness
    rep: dict[str, Any] = {"image": image, "roughness_before": _stats(rough)}
    if lo is not None or hi is not None:
        rgba[..., 1] = np.round(np.clip(rough, lo_v, hi_v) * 255).astype(np.uint8)
        pbr["roughnessFactor"] = 1.0
    if metal is not None:  # exact value via the factor; the texture channel is saturated so factor x 1 = metal
        rgba[..., 2] = 255
        pbr["metallicFactor"] = float(metal)
    rep["roughness_after"] = _stats(rgba[..., 1] / 255.0 * float(pbr.get("roughnessFactor", 1.0)))
    replaced[doc["images"][image]["bufferView"]] = _encode(rgba)
    doc["images"][image]["mimeType"] = "image/png"
    return rep


def apply_material_policy(data: bytes, policy: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    """Returns (GLB bytes, report). `policy`: MaterialPolicy fields; None/absent fields leave the material as is."""
    doc, payload = read_glb(data)
    materials = doc.get("materials") or []
    if not materials:
        raise MaterialRejected("unsupported_source_features", "the GLB has no materials to apply a policy to")
    replaced: dict[int, bytes] = {}
    per: list[dict[str, Any]] = []
    for i, mat in enumerate(materials):
        rep: dict[str, Any] = {"index": i, **_alpha(doc, payload, mat, policy)}
        if policy.get("double_sided") is not None:
            mat["doubleSided"] = bool(policy["double_sided"])
        rep["double_sided"] = bool(mat.get("doubleSided", False))
        rep.update(_metal_rough(doc, payload, mat, policy, replaced))
        per.append(rep)
    if replaced:
        payload = _repack(doc, payload, replaced)
    out = write_glb(doc, payload)
    return out, {"policy": {k: v for k, v in policy.items() if v is not None}, "materials": per,
                 "rewritten_views": sorted(replaced)}


def preservation_checks(src: bytes, out: bytes, rewritten_views: list[int]) -> list[dict[str, Any]]:
    """Independent proof: only material fields and the declared image views changed."""
    a, pa = read_glb(src)
    b, pb = read_glb(out)
    skip = {"materials", "images", "bufferViews", "buffers"}
    same_doc = {k: v for k, v in a.items() if k not in skip} == {k: v for k, v in b.items() if k not in skip}
    views_a, views_b = a.get("bufferViews") or [], b.get("bufferViews") or []
    kept = [i for i in range(len(views_a)) if i not in rewritten_views]
    same_views = len(views_a) == len(views_b) and all(
        _view_bytes(a, pa, i) == _view_bytes(b, pb, i) for i in kept)
    strip = lambda m: {k: v for k, v in m.items() if k not in MATERIAL_KEYS}  # noqa: E731
    same_mats = [strip(m) for m in a.get("materials") or []] == [strip(m) for m in b.get("materials") or []]
    img_keys = lambda im: {k: v for k, v in im.items() if k != "mimeType"}  # noqa: E731
    same_imgs = [img_keys(x) for x in a.get("images") or []] == [img_keys(x) for x in b.get("images") or []]
    return [
        {"id": "structure_unchanged", "ok": same_doc, "detail": "nodes, meshes, accessors, textures, samplers"},
        {"id": "buffers_unchanged", "ok": same_views, "detail": f"{len(kept)} buffer views byte-identical; "
                                                                f"{len(rewritten_views)} texture view(s) rewritten"},
        {"id": "material_scope", "ok": same_mats and same_imgs,
         "detail": "only alpha, culling and metallic/roughness fields changed"},
    ]
