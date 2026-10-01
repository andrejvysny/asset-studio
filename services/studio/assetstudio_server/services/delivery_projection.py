"""Deterministic legacy GLB -> descriptor / portable manifest projection (no I/O, no clocks, no app version).

Same version bytes + same profile always give byte-identical documents; a profile change must bump the profile version.
"""
from __future__ import annotations

import math
import re
from typing import Any

from assetstudio_core import canonical_v1
from assetstudio_core.delivery import (
    KNOWN_CAPABILITIES,
    AssetDescriptorV1,
    AssetRef,
    DeliveryDependency,
    DeliveryFile,
    DeliveryManifestV1,
    MaterialSlot,
    PortableSurface,
    Preparer,
)
from assetstudio_core.domain import AssetVersion

LEGACY_PROFILE = ("legacy_glb_projection", "1")
PREPARER = {"name": "assetstudio.legacy_glb_projection", "version": "1"}
SLOT_RE = re.compile(r"[^a-z0-9_.-]")
MAX_DEPTH, MAX_KEYS, MAX_LIST = 8, 256, 256


def slug(name: Any, fallback: str) -> str:
    text = SLOT_RE.sub("_", name.lower()).strip().lstrip("_.-")[:64] if isinstance(name, str) else ""
    return text or fallback


def _unique(base: str, taken: set[str]) -> str:
    cand, n = base, 1
    while cand in taken:
        n += 1
        suffix = f"_{n}"
        cand = base[:64 - len(suffix)] + suffix
    taken.add(cand)
    return cand


def material_slots(doc: dict[str, Any]) -> list[MaterialSlot]:
    """One slot per glTF material that some primitive uses; material-less primitives share a `default` slot."""
    surfaces: dict[int | None, list[PortableSurface]] = {}
    for mi, mesh in enumerate(doc.get("meshes") or []):
        for pi, prim in enumerate(mesh.get("primitives") or []):
            surfaces.setdefault(prim.get("material"), []).append(PortableSurface(mesh=mi, primitive=pi))
    materials = doc.get("materials") or []
    taken: set[str] = set()
    slots: list[MaterialSlot] = []
    order = [i for i in range(len(materials)) if i in surfaces] + ([None] if None in surfaces else [])
    for idx in order:
        if idx is None:
            sid = _unique("default", taken)
        else:
            sid = _unique(slug(materials[idx].get("name") if isinstance(materials[idx], dict) else None,
                               f"material_{idx}"), taken)
        slots.append(MaterialSlot(slot_id=sid, role="unspecified", surfaces={"portable_glb_v1": surfaces[idx]}))
    return slots


def _sanitize(value: Any, depth: int, budget: list[int]) -> Any:
    """Floats become decimal strings, non-JSON values vanish, size is bounded: contract bytes never hold floats."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return canonical_v1.decimal_str(value) if math.isfinite(value) else None
    if depth >= MAX_DEPTH:
        return None
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str) or budget[0] <= 0:
                continue
            budget[0] -= 1
            out[k] = _sanitize(v, depth + 1, budget)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize(v, depth + 1, budget) for v in list(value)[:MAX_LIST]]
    return None


def sanitize(value: dict[str, Any]) -> dict[str, Any]:
    return _sanitize(value, 0, [MAX_KEYS])


def footprint_radius(lo: list[float], hi: list[float]) -> str:
    half = [(h - low) / 2 for low, h in zip(lo, hi, strict=True)]
    radius = math.hypot(half[0], half[2]) or max(half)
    text = canonical_v1.decimal_str(radius, "ceil")
    return text if float(text) > 0 else "0.000001"


def build_descriptor(ref: AssetRef, version: AssetVersion, doc: dict[str, Any], bounds: dict[str, list[float]]
                     ) -> AssetDescriptorV1:
    lo, hi = bounds["min"], bounds["max"]
    ds = canonical_v1.decimal_str
    warnings = ["legacy_projection"]
    if any(m.get("alphaMode") == "BLEND" for m in doc.get("materials") or []):
        warnings.append("alpha_blend")
    return AssetDescriptorV1(
        schema_version=1, asset_ref=ref, kind="model3d", units="m", up_axis="+Y", forward_axis="+Z",
        bounds_min=tuple(ds(v, "floor") for v in lo), bounds_max=tuple(ds(v, "ceil") for v in hi),  # type: ignore[arg-type]
        placement_anchor=(ds((lo[0] + hi[0]) / 2), ds(lo[1]), ds((lo[2] + hi[2]) / 2)),
        footprint_radius_m=footprint_radius(lo, hi), scale_range=("0.25", "4"), height_offset_range_m=("-2", "2"),
        default_grounding="FOLLOW_TERRAIN", material_slots=material_slots(doc), collision=None,
        preview_warnings=warnings,
        source_provenance={"origin": version.origin.value, "sources": sanitize(version.sources)},
        licence=sanitize(version.licence))


def glb_capabilities(doc: dict[str, Any]) -> list[str]:
    found: set[str] = set()
    if doc.get("textures"):
        found.add("pbr_textures")
    modes = {m.get("alphaMode") for m in doc.get("materials") or [] if isinstance(m, dict)}
    found |= {"alpha_mask"} if "MASK" in modes else set()
    found |= {"alpha_blend"} if "BLEND" in modes else set()
    for mesh in doc.get("meshes") or []:
        if any("COLOR_0" in (p.get("attributes") or {}) for p in mesh.get("primitives") or []):
            found.add("vertex_colors")
    return [c for c in KNOWN_CAPABILITIES if c in found]


def build_manifest(delivery_id: str, ref: AssetRef, descriptor_sha: str, representation: str,
                   profile: tuple[str, str], preparer: dict[str, str], files: list[DeliveryFile], entrypoint: str,
                   capabilities: list[str], dependencies: list[DeliveryDependency] | None = None
                   ) -> DeliveryManifestV1:
    return DeliveryManifestV1(
        schema_version=1, delivery_id=delivery_id, asset_ref=ref, descriptor_sha256=descriptor_sha,
        representation=representation, profile_id=profile[0], profile_version=profile[1],  # type: ignore[arg-type]
        preparer=Preparer(**preparer), entrypoint=entrypoint, files=files, dependencies=dependencies or [],
        required_capabilities=capabilities)  # type: ignore[arg-type]
