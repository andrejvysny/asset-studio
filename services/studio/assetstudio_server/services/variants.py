"""Variants (Phase B1): source binding, capabilities and persisted drafts. No inference runs here."""
from __future__ import annotations

from typing import Any

from assetstudio_core.canonical import now_iso, sha256_json
from assetstudio_core.contracts import ROLE_CONTRACTS
from assetstudio_core.domain import AssetFamily, AssetManifest, AssetVersion
from assetstudio_core.ids import derived_id, new_id, validate_id
from assetstudio_core.inheritance import ResolutionError, build_snapshot
from assetstudio_core.kinds import Kind
from assetstudio_core.variants import (
    DEFAULT_CANDIDATES,
    MAX_CANDIDATES,
    MAX_ROWS,
    METHOD_LABELS,
    Constraint,
    Enforcement,
    FamilyChoice,
    GlbTransform,
    Intent,
    Method,
    RasterTransform,
    SourceArtifactRef,
    SourceBinding,
    VariantDraft,
    VariantRow,
    static_capability,
)
from assetstudio_processing.transforms import TransformRejected, inspect_static_glb
from assetstudio_storage.families import family_key
from assetstudio_storage.project import manifest_key, version_key
from assetstudio_storage.repo import IntegrityError, NotFound
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from . import commands
from . import runtime as runtime_svc

GENERATIVE_READY = False  # flips when the GPU acceptance run validates source-conditioned editing
EXPERIMENTAL_WARNING = "experimental: source-conditioned editing is not yet release-validated"
EDIT_MODEL = "qwen_image_edit_2511"
RECONSTRUCT_RECIPE = "model3d.default"
_IMAGE_ROLE_KINDS = (Kind.concept_art, Kind.sprite, Kind.icon)
MACHINE_ENFORCED_IDS: frozenset[str] = frozenset()  # constraint ids that have a real automated check (none yet)


def draft_key(draft_id: str) -> str:
    return f"variant-drafts/{draft_id}.json"


# --- source binding ----------------------------------------------------------------------------------------------
def _primary_role(kind: Kind) -> str:
    if kind is Kind.model3d:
        return "model"
    if kind in _IMAGE_ROLE_KINDS:
        return "image"
    return ROLE_CONTRACTS[kind].required[0]


def _load_source(ctx: ProjectContext, asset_id: str, version_id: str) -> tuple[AssetManifest, AssetVersion]:
    manifest = ctx.store.get_opt(manifest_key(asset_id), AssetManifest)[0]
    if manifest is None:
        raise ApiError(404, "unknown_asset", f"asset {asset_id} does not exist")
    if manifest.version(version_id) is None:
        raise ApiError(409, "source_not_published", f"version {version_id} is not a published version of {asset_id}")
    version = ctx.store.get_opt(version_key(asset_id, version_id), AssetVersion)[0]
    if version is None:
        raise ApiError(409, "source_not_published", f"version {version_id} has no record")
    return manifest, version


def _source_style_sha(ctx: ProjectContext, version: AssetVersion) -> str | None:
    if not version.config_snapshot_sha:
        return None
    try:
        style = ctx.store.read_snapshot(version.config_snapshot_sha).get("style")
    except NotFound:
        return None
    return sha256_json(style) if style is not None else None


def resolve_source(ctx: ProjectContext, asset_id: str, version_id: str, verify: bool = True) -> SourceBinding:
    manifest, version = _load_source(ctx, asset_id, version_id)
    refs: list[SourceArtifactRef] = []
    for role, ref in sorted(version.artifacts.items()):
        if verify:
            try:
                ctx.store.verify_artifact(ref["artifact_id"])
            except (IntegrityError, NotFound) as e:
                raise ApiError(409, "source_integrity_failed",
                               f"source artifact {role} failed verification: {e}") from e
        refs.append(SourceArtifactRef(role=role, artifact_id=ref["artifact_id"], sha256=ref["sha256"],
                                      size=ref["size"], mime=ref["mime"]))
    return SourceBinding(
        project_id=ctx.id, asset_id=asset_id, version_id=version_id, display_version=version.display_version,
        version_sha256=sha256_json(version.model_dump(mode="json")), kind=version.kind, origin=version.origin.value,
        display_name=manifest.display_name, primary_role=_primary_role(version.kind), artifacts=tuple(refs),
        style_sha=_source_style_sha(ctx, version), licence=version.licence)


def primary_bytes(ctx: ProjectContext, source: SourceBinding) -> bytes:
    ref = source.artifact(source.primary_role)
    if ref is None:
        raise ApiError(409, "source_integrity_failed", f"source has no {source.primary_role} artifact")
    return ctx.store.artifact_bytes(ref.artifact_id)


# --- capabilities ------------------------------------------------------------------------------------------------
def style_state(ctx: ProjectContext, category_id: str | None, source: SourceBinding) -> dict[str, Any]:
    current = None
    try:
        style = build_snapshot(ctx.config()[0], category_id, {"kind": source.kind})["style"]
        current = sha256_json(style) if style is not None else None
    except ResolutionError:
        pass
    return {"current_sha": current, "source_sha": source.style_sha,
            "conflict": source.style_sha is not None and source.style_sha != current}


def _blocker(studio: Studio, ctx: ProjectContext, source: SourceBinding, method: Method) -> tuple[str, str] | None:
    ok, why = static_capability(source.kind, method)
    if not ok:
        return ("unsupported_configuration", why)
    if method is Method.direct_transform:
        if source.kind is not Kind.model3d:
            return None
        try:
            inspect_static_glb(primary_bytes(ctx, source))
        except TransformRejected as e:
            return (e.code, str(e))
        except (IntegrityError, NotFound) as e:
            return ("corrupt_source", str(e))
        return None
    if not studio.execution.simulated:
        status = runtime_svc.model_statuses(studio).get(EDIT_MODEL)
        if status is None or not status.ready:
            return ("missing_models", f"the image-edit model {EDIT_MODEL} is not installed")
    if studio.execution.engine() is None:
        return ("engine_unavailable", "no image engine configured (library-only mode)")
    edit = _edit_workflow_blocker(studio)
    if edit is not None:
        return edit
    if method is Method.image_edit_reconstruct:
        b = runtime_svc.build_readiness(studio, RECONSTRUCT_RECIPE)
        if b["state"] != "ready":
            return (b["state"], b["reason"] or "3D reconstruction is not available")
    return None


def _edit_workflow_blocker(studio: Studio) -> tuple[str, str] | None:
    report = runtime_svc.engine_check(studio)
    if not report.get("reachable", False):
        why = "; ".join(map(str, report.get("problems") or [])) or "no answer"
        msg = f"the image engine is unreachable, so the image-edit workflow cannot run: {why}"
        return ("engine_unavailable", msg[:300])
    edits = [w for w in (report.get("workflows") or {}).values() if w.get("kind") == "image_edit"]
    if any(w.get("ready") for w in edits):
        return None
    problems = "; ".join(str(p) for w in edits for p in w.get("problems") or []) or "no image-edit workflow registered"
    return ("engine_unavailable", f"the image-edit workflow is not runnable in ComfyUI: {problems}"[:300])


def method_status(studio: Studio, ctx: ProjectContext, source: SourceBinding, method: Method) -> dict[str, Any]:
    blocked = _blocker(studio, ctx, source, method)
    warnings = [] if method is Method.direct_transform or GENERATIVE_READY else [EXPERIMENTAL_WARNING]
    return {"method": method.value, "label": METHOD_LABELS[method], "available": blocked is None,
            "reason": blocked[0] if blocked else None, "message": blocked[1] if blocked else "",
            "warnings": warnings}


def capabilities(studio: Studio, ctx: ProjectContext, asset_id: str, version_id: str) -> dict[str, Any]:
    manifest, _ = _load_source(ctx, asset_id, version_id)
    source = resolve_source(ctx, asset_id, version_id, verify=False)
    try:
        for ref in source.artifacts:
            ctx.store.verify_artifact(ref.artifact_id)
        corrupt = ""
    except (IntegrityError, NotFound) as e:
        corrupt = str(e)
    if corrupt:
        methods = [{"method": m.value, "label": METHOD_LABELS[m], "available": False, "reason": "corrupt_source",
                    "message": corrupt, "warnings": []} for m in Method]
    else:
        methods = [method_status(studio, ctx, source, m) for m in Method]
    fam = ctx.store.get_opt(family_key(manifest.family_id), AssetFamily)[0] if manifest.family_id else None
    return {"source": source.model_dump(mode="json"),
            "family": {"id": fam.id, "name": fam.name} if fam else None,
            "style": style_state(ctx, manifest.category_id, source), "methods": methods}


# --- request models ----------------------------------------------------------------------------------------------
class RowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    label: str = Field(min_length=1, max_length=120)
    change_request: str = Field(default="", max_length=2000)
    final_height_m: float | None = None
    glb_transform: GlbTransform | None = None
    raster_transform: RasterTransform | None = None
    candidate_count: int | None = Field(default=None, ge=1, le=MAX_CANDIDATES)
    confirm_duplicate: bool = False


class CreateDraft(BaseModel):
    asset_id: str
    version_id: str
    method: Method
    intent: Intent = Intent.related
    requested_variants: int = Field(default=1, ge=1, le=MAX_ROWS)
    candidates_per_variant: int = Field(default=DEFAULT_CANDIDATES, ge=1, le=MAX_CANDIDATES)
    change_request: str = Field(default="", max_length=2000)
    rows: list[RowIn] = Field(default=[], max_length=MAX_ROWS)
    family_name: str | None = Field(default=None, min_length=1, max_length=120)
    idempotency_key: str = Field(min_length=8, max_length=100)


class PatchDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int
    method: Method | None = None
    intent: Intent | None = None
    preserve: list[Constraint] | None = None
    rows: list[RowIn] | None = Field(default=None, max_length=MAX_ROWS)
    candidates_per_row: int | None = Field(default=None, ge=1, le=MAX_CANDIDATES)
    request: str | None = Field(default=None, max_length=4000)
    family_name: str | None = Field(default=None, min_length=1, max_length=120)
    primary_view: str | None = Field(default=None, min_length=1, max_length=40)
    style_ack: bool | None = None


def to_rows(rows: list[RowIn], default_candidates: int) -> list[VariantRow]:
    out: list[VariantRow] = []
    for r in rows:
        data = r.model_dump(exclude={"id", "candidate_count"}, exclude_none=True)
        data["candidate_count"] = r.candidate_count or default_candidates
        try:
            out.append(VariantRow(id=r.id or new_id("row"), **data))
        except (ValidationError, ValueError) as e:
            raise ApiError(422, "invalid_row", f"row {r.label!r}: {e}") from e
    return out


# --- drafts ------------------------------------------------------------------------------------------------------
def load_draft(ctx: ProjectContext, draft_id: str) -> tuple[VariantDraft, str]:
    validate_id(draft_id, "vdr")
    try:
        return ctx.store.get(draft_key(draft_id), VariantDraft)
    except NotFound as e:
        raise ApiError(404, "unknown_draft", f"variant draft {draft_id} does not exist") from e


def save_draft(ctx: ProjectContext, draft: VariantDraft, token: str) -> VariantDraft:
    draft.revision += 1
    draft.updated_at = now_iso()
    ctx.store.replace(draft_key(draft.id), draft, token)
    return draft


def _plan_draft(ctx: ProjectContext, req: CreateDraft, cid: str) -> dict[str, Any]:
    validate_id(req.asset_id, "ast")
    validate_id(req.version_id, "ver")
    source = resolve_source(ctx, req.asset_id, req.version_id)
    ok, why = static_capability(source.kind, req.method)
    if not ok:
        raise ApiError(422, "unsupported_configuration", why)
    manifest = ctx.store.get(manifest_key(req.asset_id), AssetManifest)[0]
    family = (FamilyChoice(family_id=manifest.family_id) if manifest.family_id
              else FamilyChoice(new_name=req.family_name or source.display_name))
    rows = to_rows(req.rows, req.candidates_per_variant)
    if not rows and req.requested_variants == 1:
        rows = to_rows([RowIn(label="Variant", change_request=req.change_request)], req.candidates_per_variant)
    now = now_iso()
    direct = req.method is Method.direct_transform
    draft = VariantDraft(id=derived_id("vdr", cid), project_id=ctx.id, source=source, method=req.method,
                         intent=None if direct else req.intent, rows=rows, family=family,
                         candidates_per_row=req.candidates_per_variant, request=req.change_request,
                         created_at=now, updated_at=now)
    return {"draft": draft.model_dump(mode="json")}


@commands.replayable("variant_draft_create")
def _draft_effects(studio: Studio, ctx: ProjectContext, plan: dict[str, Any], cid: str) -> dict[str, Any]:
    draft = VariantDraft.model_validate(plan["draft"])
    if ctx.store.repo.stat_object(draft_key(draft.id)) is None:
        ctx.store.create(draft_key(draft.id), draft)
    return {"draft_id": draft.id}


def create_draft(studio: Studio, ctx: ProjectContext, req: CreateDraft) -> VariantDraft:
    res = commands.execute(studio, ctx, "variant_draft_create", req.idempotency_key, req.model_dump(mode="json"),
                           lambda cid: _plan_draft(ctx, req, cid))
    return load_draft(ctx, res["draft_id"])[0]


def _check_enforcement(preserve: list[Constraint]) -> None:
    for c in preserve:
        if c.enforcement is Enforcement.machine_enforced and c.id not in MACHINE_ENFORCED_IDS:
            raise ApiError(422, "enforcement_unsupported",
                           "no machine-enforced check exists for this constraint; use advisory_visual")


def _apply_patch(draft: VariantDraft, req: PatchDraft) -> None:
    if req.method is not None and req.method != draft.method:
        ok, why = static_capability(draft.source.kind, req.method)
        if not ok:
            raise ApiError(422, "unsupported_configuration", why)
        draft.method = req.method
        draft.intent = None if req.method is Method.direct_transform else (draft.intent or Intent.related)
    if req.intent is not None and draft.method is not Method.direct_transform:
        draft.intent = req.intent
    if req.preserve is not None:
        _check_enforcement(req.preserve)
        draft.preserve = req.preserve
    if req.candidates_per_row is not None:
        draft.candidates_per_row = req.candidates_per_row
    if req.rows is not None:
        draft.rows = to_rows(req.rows, draft.candidates_per_row)
    if req.request is not None:
        draft.request = req.request
    if req.family_name is not None:
        if draft.family.family_id is not None:
            raise ApiError(422, "family_fixed", "the source already belongs to a family; it cannot be renamed here")
        draft.family = FamilyChoice(new_name=req.family_name)
    if req.primary_view is not None:
        draft.primary_view = req.primary_view
    if req.style_ack is not None:
        draft.style_ack = req.style_ack


def patch_draft(ctx: ProjectContext, draft_id: str, req: PatchDraft) -> VariantDraft:
    ctx.require_writable()
    with ctx.store.lock:
        draft, token = load_draft(ctx, draft_id)
        if draft.materialized is not None:
            raise ApiError(409, "draft_materialized", "this draft was already saved as Jobs; start a new draft")
        if draft.revision != req.expected_revision:
            raise ApiError(409, "stale_variant_plan", f"draft changed (revision {draft.revision}); reload")
        _apply_patch(draft, req)
        return save_draft(ctx, draft, token)
