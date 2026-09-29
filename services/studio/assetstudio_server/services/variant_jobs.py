"""Variants (Phase B1): freeze an immutable VariantPlan and save one one-item Job per row (+ a draft Batch when
more than one). Nothing is queued and no inference runs: Jobs start only through an explicit run."""
from __future__ import annotations

from typing import Any

from assetstudio_core.canonical import now_iso, sha256_json
from assetstudio_core.domain import AssetManifest
from assetstudio_core.ids import derived_id, validate_id
from assetstudio_core.inheritance import ResolutionError, build_snapshot
from assetstudio_core.seeds import derive_seed, new_seed_family
from assetstudio_core.variants import (
    FamilyChoice,
    Method,
    ReferenceImage,
    SourceBinding,
    VariantContext,
    VariantDraft,
    VariantPlan,
    VariantRow,
    row_candidates,
    static_capability,
    validate_rows,
    work_summary,
)
from assetstudio_storage.families import FamilyConflict, attach_asset, create_family
from assetstudio_storage.project import manifest_key
from assetstudio_storage.repo import Conflict, NotFound
from pydantic import BaseModel, Field

from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from . import commands, runs
from . import jobs as jsvc
from .variant_refs import load_reference_set
from .variants import load_draft, method_status, resolve_source, save_draft, style_state

STYLE_CONFLICT_MESSAGE = ("The source was created under an earlier project style. New generated variants use the "
                          "current style; exact appearance matching may conflict.")
GENERATION_ROUTE = "comfyui.qwen_edit_2511"


class CreateJobs(BaseModel):
    expected_revision: int
    idempotency_key: str = Field(min_length=8, max_length=100)


def plan_key(plan_id: str) -> str:
    return f"variant-plans/{plan_id}.json"


def load_plan(ctx: ProjectContext, plan_id: str) -> VariantPlan:
    """The frozen plan record behind a variant Job (never re-derived from a mutable draft)."""
    try:
        raw = ctx.store.repo.read_object(plan_key(plan_id)).data
    except NotFound as e:
        raise ApiError(409, "unknown_plan", f"variant plan {plan_id} is missing") from e
    return VariantPlan.model_validate_json(raw)


def _family_plan(manifest: AssetManifest, choice: FamilyChoice, cid: str) -> dict[str, Any]:
    if choice.family_id is not None:
        if manifest.family_id != choice.family_id:
            raise ApiError(409, "source_family_changed", "the source's family changed since the draft was made")
        return {"id": choice.family_id, "name": None, "new": False}
    if manifest.family_id is not None:
        raise ApiError(409, "source_family_changed",
                       "the source already belongs to a family; create a new draft to join it")
    return {"id": derived_id("fam", cid), "name": choice.new_name, "new": True}


def _checked_draft(ctx: ProjectContext, draft_id: str, req: CreateJobs) -> tuple[VariantDraft, SourceBinding]:
    draft, _ = load_draft(ctx, draft_id)
    if draft.materialized is not None:
        raise ApiError(409, "draft_materialized", "this draft was already saved as Jobs")
    if draft.revision != req.expected_revision:
        raise ApiError(409, "stale_variant_plan", f"draft changed (revision {draft.revision}); reload")
    source = resolve_source(ctx, draft.source.asset_id, draft.source.version_id)
    if source.content_key() != draft.source.content_key():
        raise ApiError(409, "source_version_mismatch", "the source no longer matches the draft; start a new draft")
    if not draft.rows:
        raise ApiError(422, "empty_plan", "add at least one variant row")
    return draft, source


def _check_method(studio: Studio, ctx: ProjectContext, draft: VariantDraft, source: SourceBinding) -> None:
    ok, why = static_capability(source.kind, draft.method)
    if not ok:
        raise ApiError(422, "unsupported_configuration", why)
    st = method_status(studio, ctx, source, draft.method)  # re-checks engine/edit-workflow readiness at save time
    if not st["available"]:
        raise ApiError(422, st["reason"], st["message"])
    errors = validate_rows(draft.method, source.kind, draft.rows)
    if errors:
        raise ApiError(422, "conflicting_variant_requirements", "some rows have conflicting requirements", errors)


def _plan_record(draft: VariantDraft, source: SourceBinding, family: dict[str, Any], refs: list[ReferenceImage],
                 style: dict[str, Any], cid: str) -> dict[str, Any]:
    direct = draft.method is Method.direct_transform
    plan = VariantPlan(
        id=derived_id("vpl", cid), draft_id=draft.id, project_id=draft.project_id, source=source,
        method=draft.method, intent=draft.intent, output_kind=source.kind, preserve=tuple(draft.preserve),
        rows=tuple(draft.rows),
        family=FamilyChoice(family_id=family["id"]) if not family["new"] else FamilyChoice(new_name=family["name"]),
        references=tuple(refs), reference_set_id=draft.reference_set_id,
        style={**style, "acknowledged": draft.style_ack,
               "application": "source_preserved" if direct else "current_project_style"},
        planner={"mode": "suggested" if draft.suggestion else "manual", "human_reviewed": True,
                 "seed_family": new_seed_family()},
        created_at=now_iso())
    body = plan.model_dump(mode="json")
    body["sha256"] = sha256_json({k: v for k, v in body.items() if k != "sha256"})
    return body


def _row_job(ctx: ProjectContext, manifest: AssetManifest, draft: VariantDraft, source: SourceBinding,
             plan: dict[str, Any], family_id: str, row: VariantRow, cid: str) -> dict[str, Any]:
    cfg, _ = ctx.config()
    direct = draft.method is Method.direct_transform
    try:
        snap = build_snapshot(cfg, manifest.category_id, {"kind": source.kind,
                                                          "candidate_count": row_candidates(draft.method, row)})
    except ResolutionError as e:
        raise ApiError(422, "unsupported_configuration", str(e)) from e
    snap = {k: v for k, v in snap.items() if k != "sha256"}
    snap["variant"] = {"method": draft.method.value, "intent": draft.intent.value if draft.intent else None,
                       "plan_id": plan["id"], "plan_sha256": plan["sha256"], "row_id": row.id,
                       "generation": None if direct else GENERATION_ROUTE}
    snap["sha256"] = sha256_json(snap)
    name = f"{source.display_name} — {row.label}"
    context = VariantContext(
        plan_id=plan["id"], plan_sha256=plan["sha256"], row_id=row.id, family_id=family_id, method=draft.method,
        intent=draft.intent, source_asset_id=source.asset_id, source_version_id=source.version_id,
        source_display_version=source.display_version, source_name=source.display_name,
        change_request=row.change_request, final_height_m=row.final_height_m)
    return {"job_id": derived_id("job", cid, row.id), "recipe_id": snap["recipe"]["id"], "title": name,
            "category_id": manifest.category_id, "config_revision": cfg.revision, "source": "variant",
            "seed_family": derive_seed(plan["planner"]["seed_family"], row.id), "direct": direct,
            "variant": context.model_dump(mode="json"),
            "items": [{"id": derived_id("itm", cid, row.id), "name": name, "brief": row.change_request,
                       "category_id": snap["category_id"], "shot_id": None, "target_asset_id": None,
                       "snapshot": snap}]}


def _plan(studio: Studio, ctx: ProjectContext, draft_id: str, req: CreateJobs, cid: str) -> dict[str, Any]:
    draft, source = _checked_draft(ctx, draft_id, req)
    manifest = ctx.store.get(manifest_key(source.asset_id), AssetManifest)[0]
    _check_method(studio, ctx, draft, source)
    generative = draft.method is not Method.direct_transform
    refs: list[ReferenceImage] = load_reference_set(ctx, draft)[1] if generative else []
    style = style_state(ctx, manifest.category_id, source)
    if generative and style["conflict"] and not draft.style_ack:
        raise ApiError(409, "style_source_conflict", STYLE_CONFLICT_MESSAGE, style)
    family = _family_plan(manifest, draft.family, cid)
    plan = _plan_record(draft, source, family, refs, style, cid)
    jobs = [_row_job(ctx, manifest, draft, source, plan, family["id"], r, cid) for r in draft.rows]
    batch = None
    if len(jobs) > 1:
        batch = {"batch_id": derived_id("bch", cid), "title": f"Variants · {source.display_name}",
                 "job_ids": [j["job_id"] for j in jobs]}
    return {"draft_id": draft_id, "plan": plan, "family": family, "jobs": jobs, "batch": batch,
            "summary": work_summary(draft.method, draft.rows)}


def _ensure_family(ctx: ProjectContext, p: dict[str, Any], cid: str) -> str | None:
    """Create-or-join the family and attach the source. -> conflict message, or None when consistent."""
    fam, src = p["family"], p["plan"]["source"]
    manifest = ctx.store.get(manifest_key(src["asset_id"]), AssetManifest)[0]
    if manifest.family_id != fam["id"] and (not fam["new"] or manifest.family_id is not None):
        return f"source {src['asset_id']} belongs to another family; no Jobs were saved"
    if not fam["new"]:
        return None
    try:
        record = create_family(ctx.store, family_id=fam["id"], name=fam["name"], kind=manifest.kind,
                               anchor_asset_id=src["asset_id"], anchor_version_id=src["version_id"], op_id=cid)
        attached = attach_asset(ctx.store, asset_id=src["asset_id"], family_id=fam["id"],
                                expected_manifest_revision=None)
    except (FamilyConflict, Conflict) as e:
        return str(e)
    ctx.index.upsert(attached, record.name)
    return None


def _mark_materialized(ctx: ProjectContext, p: dict[str, Any], batch_id: str | None) -> None:
    with ctx.store.lock:
        draft, token = load_draft(ctx, p["draft_id"])
        if draft.materialized is None:
            draft.materialized = {"plan_id": p["plan"]["id"], "job_ids": [j["job_id"] for j in p["jobs"]],
                                  "batch_id": batch_id, "family_id": p["family"]["id"]}
            save_draft(ctx, draft, token)


@commands.replayable("variant_create_jobs")
def _effects(studio: Studio, ctx: ProjectContext, p: dict[str, Any], cid: str) -> dict[str, Any]:
    """Idempotent: derived ids only, create-or-same records; nothing is queued."""
    conflict = _ensure_family(ctx, p, cid)
    if conflict is not None:
        return {"conflict": {"code": "source_family_changed", "message": conflict}}
    plan = p["plan"]
    ctx.store.create_or_same(plan_key(plan["id"]), plan)
    for j in p["jobs"]:
        jsvc.write_job(ctx, j)
    batch_id = None
    if p["batch"]:
        batch_id = runs.write_batch_group(ctx, p["batch"]["batch_id"], p["batch"]["title"],
                                          p["batch"]["job_ids"]).id
        studio.events.publish("batch", project_id=ctx.id, batch_id=batch_id)
    _mark_materialized(ctx, p, batch_id)
    for j in p["jobs"]:
        studio.events.publish("job", project_id=ctx.id, job_id=j["job_id"])
    studio.events.publish("library", project_id=ctx.id)
    return {"plan_id": plan["id"], "family_id": p["family"]["id"], "job_ids": [j["job_id"] for j in p["jobs"]],
            "batch_id": batch_id, "summary": p["summary"]}


def create_jobs(studio: Studio, ctx: ProjectContext, draft_id: str, req: CreateJobs) -> dict[str, Any]:
    validate_id(draft_id, "vdr")
    body = {"draft_id": draft_id, **req.model_dump(mode="json")}
    res = commands.execute(studio, ctx, "variant_create_jobs", req.idempotency_key, body,
                           lambda cid: _plan(studio, ctx, draft_id, req, cid))
    if "conflict" in res:
        raise ApiError(409, res["conflict"]["code"], res["conflict"]["message"])
    return res
