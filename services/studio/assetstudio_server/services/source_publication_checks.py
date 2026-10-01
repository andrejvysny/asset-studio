"""Validation helpers for static-source publication previews: every claim of the upload is rechecked here."""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from assetstudio_core.canonical_v1 import canonical_bytes, decimal_str
from assetstudio_core.delivery import strict_loads
from assetstudio_core.publication_draft import DescriptorDraftV1, parse_draft
from assetstudio_core.source_manifest import ConversionReport, SourcePackageManifestV1
from assetstudio_processing.glb import validate_glb_bytes
from assetstudio_processing.glb_budget import glb_budget, glb_json
from assetstudio_processing.images import ImageRejected, inspect_image
from assetstudio_processing.source_package import SourcePackageReport, validate_source_package
from assetstudio_processing.transforms import TransformRejected, inspect_static_glb
from pydantic import ValidationError

from ..integration_api.errors import IntegrationError
from ..integration_api.principal import Principal
from ..registry import ProjectContext
from ..studio import Studio
from . import deliveries as svc

SLUG = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
MAX_PROBLEMS = 20
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PLACEMENT_FIELDS = ("placement_anchor", "footprint_radius_m", "scale_range", "height_offset_range_m",
                    "default_grounding")


def invalid(message: str, detail: str, **extra: Any) -> IntegrationError:
    return IntegrationError(422, "invalid_request", message, details={"detail": detail, **extra})


@dataclass
class GlbFacts:
    doc: dict[str, Any]
    bounds_min: list[str]
    bounds_max: list[str]
    budget: dict[str, Any]
    warnings: list[str]


def check_glb(data: bytes, limits: dict[str, Any]) -> GlbFacts:
    """Static, self-contained GLB only (no skins/animation/external buffers); bounds are recomputed here."""
    result = validate_glb_bytes(data, require_texture=False)
    if not result["ok"]:
        failed = [{"id": c.get("id"), "detail": str(c.get("detail", ""))[:200]}
                  for c in result["checks"] if not c.get("ok")][:MAX_PROBLEMS]
        raise IntegrationError(422, "unsafe_package", "portable GLB failed validation", details={"checks": failed})
    try:
        info = inspect_static_glb(data)
    except TransformRejected as e:
        raise IntegrationError(422, "unsafe_package", "portable GLB is not a static model",
                               details={"detail": str(e)[:300]}) from None
    doc = glb_json(data)[0]
    budget = glb_budget(data, limits)
    warnings = [] if budget["within_ipad_budget"] else ["portable_over_ipad_budget"]
    return GlbFacts(doc, [decimal_str(v, "floor") for v in info["bounds"]["min"]],
                    [decimal_str(v, "ceil") for v in info["bounds"]["max"]], budget, warnings)


def parse_draft_part(raw: bytes) -> DescriptorDraftV1:
    try:
        return parse_draft(raw)
    except ValidationError as e:
        errs = [{"path": ".".join(str(p) for p in err["loc"]), "message": err["msg"]}
                for err in e.errors()[:MAX_PROBLEMS]]
        raise invalid("descriptor draft failed validation", "draft_invalid", errors=errs) from None
    except (ValueError, RecursionError) as e:
        raise invalid("descriptor draft is not valid contract JSON", "draft_invalid", errors=[str(e)[:200]]) from None


def check_surfaces(draft: DescriptorDraftV1, doc: dict[str, Any]) -> None:
    meshes = doc.get("meshes") or []
    for slot in draft.material_slots:
        for surface in slot.surfaces["portable_glb_v1"]:
            prims = meshes[surface.mesh].get("primitives") or [] if surface.mesh < len(meshes) else []
            if surface.primitive >= len(prims):
                raise invalid("a portable surface does not exist in the GLB", "surface_missing",
                              slot_id=slot.slot_id, mesh=surface.mesh, primitive=surface.primitive)


def check_anchor(draft: DescriptorDraftV1, facts: GlbFacts) -> None:
    radius = Decimal(draft.footprint_radius_m)
    for axis, a in enumerate(draft.placement_anchor):
        if not Decimal(facts.bounds_min[axis]) - radius <= Decimal(a) <= Decimal(facts.bounds_max[axis]) + radius:
            raise invalid("placement_anchor lies outside the model bounds plus footprint_radius_m", "anchor_outside",
                          axis="xyz"[axis])


def check_thumbnail(data: bytes) -> None:
    if not data.startswith(PNG_MAGIC):
        raise invalid("thumbnail must be a PNG", "thumbnail_invalid")
    try:
        inspect_image(data, ("PNG",))
    except ImageRejected as e:
        raise invalid("thumbnail is not a decodable PNG", "thumbnail_invalid", reason=str(e)[:200]) from None


def parse_report(raw: bytes) -> ConversionReport:
    try:
        return ConversionReport.model_validate(strict_loads(raw))
    except (ValueError, RecursionError) as e:
        raise invalid("conversion report is invalid", "report_invalid", reason=str(e)[:200]) from None


# --- source package -------------------------------------------------------------------------------------------------
@dataclass
class SourceFacts:
    report: SourcePackageReport
    manifest: SourcePackageManifestV1
    warnings: list[str]


def dependency_resolver(studio: Studio, ctx: ProjectContext, who: Principal, server_id: str,
                        limits: dict[str, Any]) -> Callable[[str, dict[str, Any]], str | None]:
    """Resolver for source asset dependencies: only libraries this token may read; existence is never disclosed."""

    def resolve(_key: str, dep: dict[str, Any]) -> str | None:
        ref = dep["asset_ref"]
        if ref["server_id"] != server_id or "assets:read" not in who.scopes or ref["library_id"] not in who.library_ids:
            return "forbidden_dependency"
        try:
            dep_ctx = ctx if ref["library_id"] == ctx.id else studio.registry.get(ref["library_id"])
        except Exception:  # noqa: BLE001 - an unopenable library is simply unavailable
            return "dependency library unavailable"
        try:
            found = svc.ensure_version(studio, dep_ctx, server_id, ref["asset_id"], ref["version_id"], limits)
        except Exception:  # noqa: BLE001 - any lookup/storage failure means unavailable
            return "dependency version not found"
        return _dependency_problem(found, dep)

    return resolve


def _dependency_problem(found: svc.EnsureResult, dep: dict[str, Any]) -> str | None:
    if found.state != "ready" or found.descriptor is None:
        return "dependency is not ready"
    if found.descriptor[0].descriptor_sha256 != dep["descriptor_sha256"]:
        return "dependency descriptor hash differs from the declared one"
    matching = [d for d in found.deliveries if d.representation == dep["representation"]]
    if not matching:
        return "dependency has no delivery of the declared representation"
    if dep["delivery_id"] is not None and dep["delivery_id"] not in {d.delivery_id for d in matching}:
        return "declared dependency delivery does not exist"
    return None


def check_source(zip_path: Path, work_dir: Path, capabilities: dict[str, Any],
                 resolver: Callable[[str, dict[str, Any]], str | None]) -> SourceFacts:
    report = validate_source_package(zip_path, work_dir, capabilities=capabilities, resolve_dependency=resolver)
    if not report.ok or report.manifest is None:
        problems = [{"code": p.code, "detail": p.detail, "path": p.path, "message": p.message[:300]}
                    for p in report.errors[:MAX_PROBLEMS]]
        first = report.errors[0]
        raise IntegrationError(422, first.code, first.message[:300], details={"problems": problems})
    warnings = sorted({w.detail for w in report.warnings if SLUG.match(w.detail)})
    return SourceFacts(report, report.manifest, warnings)


def check_agreement(draft: DescriptorDraftV1, facts: SourceFacts, report: ConversionReport) -> dict[str, list[Any]]:
    """The draft and the package state the same placement (one source of truth). Returns slot_id -> source surfaces."""
    manifest = facts.manifest
    placement = manifest.placement.model_dump(mode="json")
    declared = draft.model_dump(mode="json")
    for name in PLACEMENT_FIELDS:
        if declared[name] != placement[name]:
            raise invalid("descriptor draft and source manifest disagree on placement", "placement_mismatch",
                          field=name)
    by_id = {s["slot_id"]: s for s in placement["material_slots"]}
    if [s.slot_id for s in draft.material_slots] != [s["slot_id"] for s in placement["material_slots"]]:
        raise invalid("draft material slots differ from the source manifest", "slot_mismatch")
    surfaces: dict[str, list[Any]] = {}
    for slot in draft.material_slots:
        want = by_id[slot.slot_id]
        mine = [s.model_dump(mode="json") for s in slot.surfaces.get("godot_static_source_v1", [])]
        if slot.role != want["role"] or (mine and mine != want["source_surfaces"]):
            raise invalid("draft slot differs from the source manifest", "slot_mismatch", slot_id=slot.slot_id)
        surfaces[slot.slot_id] = want["source_surfaces"]
    if draft.collision is not None and "static_collision" not in facts.report.detected_capabilities:
        raise invalid("draft declares collision but the package has none", "collision_mismatch")
    if canonical_bytes(report.model_dump(mode="json")) != canonical_bytes(
            manifest.conversion_report.model_dump(mode="json")):
        raise invalid("conversion report differs from the source manifest", "report_mismatch")
    return surfaces


def report_warnings(report: ConversionReport | None) -> list[str]:
    if report is None:
        return []
    return {"approximated": ["custom_shader_approximated"], "desktop_only": ["portable_desktop_only"]}.get(
        report.portable_status, [])


def check_draft_without_source(draft: DescriptorDraftV1) -> None:
    if draft.collision is not None or any("godot_static_source_v1" in s.surfaces for s in draft.material_slots):
        raise invalid("source surfaces and collision need a source package", "source_required")

