"""Jobs: configured production workflows of one or more items (formerly "batches"). Creating a Job saves it;
inference starts only through an explicit run (standalone or as part of a Batch)."""
from __future__ import annotations

from typing import Any

from assetstudio_core.canonical import now_iso, sha256_json
from assetstudio_core.domain import AssetFamily, BuildRun, Job, JobItem, ShotItem
from assetstudio_core.ids import derived_id
from assetstudio_core.inheritance import ResolutionError, build_snapshot
from assetstudio_core.kinds import KINDS, Kind
from assetstudio_core.lifecycle import aggregate, next_action, progress
from assetstudio_core.recipes import RECIPES, legacy_variant
from assetstudio_storage.families import family_key
from assetstudio_storage.project import job_descriptor_key
from pydantic import BaseModel, Field

from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from . import commands
from .jobviews import item_view
from .records import item_key, load_build, load_items, load_job
from .references import NewReference, Preset, resolve_new
from .taskview import item_tasks


class NewItem(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    brief: str = Field(default="", max_length=4000)
    category_id: str | None = None
    kind: Kind | None = None
    shot_id: str | None = None
    target_asset_id: str | None = None
    references: list[NewReference] = []
    enhance_preset: Preset | None = None  # None: the Job's default


class CreateJob(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    category_id: str | None = None
    kind: Kind | None = None
    items: list[NewItem] = Field(min_length=1, max_length=200)
    candidate_count: int | None = Field(default=None, ge=1, le=8)
    seed_family: int | None = Field(default=None, ge=0, le=2**53 - 1)
    enhance_preset: Preset = "conservative"
    source: str = Field(default="ad hoc", max_length=120)
    idempotency_key: str = Field(min_length=8, max_length=100)


def job_ids(ctx: ProjectContext) -> list[str]:
    """All Job ids: legacy `bat_` Jobs under batches/, new `job_` Jobs under jobs/."""
    ids: list[str] = []
    for prefix, descriptor in (("batches", "batch.json"), ("jobs", "job.json")):
        cursor: str | None = None
        while True:
            keys, cursor = ctx.store.repo.list_keys(prefix, cursor)
            ids += [k.split("/")[1] for k in keys if k.endswith(f"/{descriptor}") and k.count("/") == 2]
            if cursor is None:
                break
    return ids


def active_shot_claims(ctx: ProjectContext) -> dict[str, tuple[str, str]]:
    """shot id -> (job id, item id) for unpublished, uncancelled items."""
    claims: dict[str, tuple[str, str]] = {}
    for jid in job_ids(ctx):
        job, _ = load_job(ctx.store, jid)
        for item in load_items(ctx.store, job):
            if item.shot_id and not item.cancelled and item.published is None:
                claims[item.shot_id] = (job.id, item.id)
    return claims


def _plan_create(ctx: ProjectContext, req: CreateJob, cid: str) -> dict[str, Any]:
    cfg, _ = ctx.config()
    shots = {s.id: s for s in ctx.store.read_shotlist()[0].items}
    snapshots: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for i, it in enumerate(req.items):
        cat = it.category_id or req.category_id
        if cat is not None and cfg.category(cat) is None:
            errors.append({"row": i, "name": it.name, "message": f"unknown category {cat!r}"})
            continue
        try:
            snapshots.append(build_snapshot(cfg, cat, {"kind": it.kind or req.kind,
                                                       "candidate_count": req.candidate_count}))
        except ResolutionError as e:
            errors.append({"row": i, "name": it.name, "message": str(e)})
        if it.shot_id is not None and it.shot_id not in shots:
            errors.append({"row": i, "name": it.name, "message": f"unknown shot {it.shot_id}"})
    if errors:
        raise ApiError(422, "invalid_items", "some items cannot be planned", errors)
    recipes = {s["recipe"]["id"] for s in snapshots}
    if len(recipes) != 1:
        by = {r: [req.items[i].name for i, s in enumerate(snapshots) if s["recipe"]["id"] == r] for r in recipes}
        raise ApiError(422, "mixed_kinds", "a Job holds one output kind/recipe; split the selection into Jobs", by)
    claims = active_shot_claims(ctx)
    taken = [{"shot_id": it.shot_id, "name": it.name, "job_id": claims[it.shot_id][0]}
             for it in req.items if it.shot_id in claims]
    if taken:
        raise ApiError(409, "shot_claimed", "some shot-list rows are already in an active Job", taken)
    job_id = derived_id("job", cid)
    items = []
    for n, (it, snap) in enumerate(zip(req.items, snapshots, strict=True)):
        shot: ShotItem | None = shots.get(it.shot_id or "")
        item_id = derived_id("itm", cid, str(n))
        items.append({"id": item_id, "name": it.name, "references": resolve_new(ctx, item_id, it.references),
                      "enhance_preset": it.enhance_preset or req.enhance_preset,
                      "brief": it.brief or (shot.brief if shot else ""), "category_id": snap["category_id"],
                      "shot_id": it.shot_id, "target_asset_id": it.target_asset_id or (
                          shot.target_asset_id if shot else None), "snapshot": snap})
    return {"job_id": job_id, "recipe_id": recipes.pop(), "title": req.title, "category_id": req.category_id,
            "seed_family": req.seed_family if req.seed_family is not None else _seed(), "source": req.source,
            "config_revision": cfg.revision, "items": items}


def _seed() -> int:
    from assetstudio_core.seeds import new_seed_family

    return new_seed_family()


def write_job(ctx: ProjectContext, plan: dict[str, Any]) -> None:
    """Idempotent save of one Job + its items + snapshots from a frozen plan (no events, nothing queued).
    Optional plan keys: `variant` (Job.variant dict) and `direct` (bool)."""
    job_id = plan["job_id"]
    if ctx.store.repo.stat_object(job_descriptor_key(job_id)) is not None:
        return
    now = now_iso()
    with ctx.store.lock:
        for it in plan["items"]:
            snap = it["snapshot"]
            ctx.store.save_snapshot(snap)
            item = JobItem(id=it["id"], job_id=job_id, name=it["name"], brief=it["brief"],
                           category_id=it["category_id"], shot_id=it["shot_id"],
                           target_asset_id=it["target_asset_id"], snapshot_sha=snap["sha256"], created_at=now,
                           updated_at=now, references=it.get("references", []),
                           references_revision=1 if it.get("references") else 0,
                           enhance_preset=it.get("enhance_preset", "conservative"))
            if ctx.store.repo.stat_object(item_key(job_id, item.id)) is None:
                ctx.store.create(item_key(job_id, item.id), item)
        recipe = RECIPES[plan["recipe_id"]]
        job = Job(id=job_id, alias=f"J-{len(job_ids(ctx)) + 1:04d}", title=plan["title"], kind=recipe.kind,
                  recipe_id=recipe.id, category_id=plan["category_id"], created_at=now,
                  seed_family=plan["seed_family"], item_ids=[i["id"] for i in plan["items"]],
                  source=plan["source"], config_revision=plan["config_revision"],
                  variant=plan.get("variant"), direct=plan.get("direct", False))
        ctx.store.create(job_descriptor_key(job_id), job)


@commands.replayable("job_create")
def _create_effects(studio: Studio, ctx: ProjectContext, plan: dict[str, Any], cid: str) -> dict[str, Any]:
    """Save only. Every id is derived from the command: a replay writes the same Job, never a second one."""
    job_id = plan["job_id"]
    write_job(ctx, plan)
    studio.events.publish("job", project_id=ctx.id, job_id=job_id)
    return {"job_id": job_id, "batch_id": job_id}


def create_job(studio: Studio, ctx: ProjectContext, req: CreateJob) -> tuple[Job, bool]:
    """-> (job, created by this call). A replay with the same key returns the same Job."""
    existed = ctx.store.repo.stat_object(job_descriptor_key(derived_id(
        "job", commands.command_id(ctx, "job_create", req.idempotency_key)))) is not None
    res = commands.execute(studio, ctx, "job_create", req.idempotency_key, req.model_dump(mode="json"),
                           lambda cid: _plan_create(ctx, req, cid))
    return load_job(ctx.store, res["job_id"])[0], not existed


# --- read models ---------------------------------------------------------------------------------------------------
def _builds(ctx: ProjectContext, job: Job, items: list[JobItem]) -> dict[str, BuildRun]:
    return {it.current_build: load_build(ctx.store, job.id, it.current_build)[0] for it in items if it.current_build}


def batch_index(ctx: ProjectContext) -> dict[str, dict[str, str]]:
    """job id -> grouping Batch {id, title} (first Batch that lists it)."""
    from .runs import batch_ids, load_batch_group

    out: dict[str, dict[str, str]] = {}
    for bid in batch_ids(ctx):
        batch = load_batch_group(ctx, bid)[0]
        for jid in batch.job_ids:
            out.setdefault(jid, {"id": batch.id, "title": batch.title})
    return out


def _family_ref(ctx: ProjectContext, job: Job) -> dict[str, str] | None:
    fid = (job.variant or {}).get("family_id")
    if not fid:
        return None
    fam = ctx.store.get_opt(family_key(fid), AssetFamily)[0]
    return {"id": fid, "name": fam.name if fam else ""}


def job_summary(studio: Studio, ctx: ProjectContext, job: Job,
                batches: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    from .runs import active_run_for

    items = load_items(ctx.store, job)
    tasks = {i.id: item_tasks(studio, ctx.id, i) for i in items}
    agg = aggregate(items, _builds(ctx, job, items), tasks, job.direct)
    cat = ctx.config()[0].category(job.category_id) if job.category_id else None
    snap = ctx.store.read_snapshot(items[0].snapshot_sha) if items else None
    return {"id": job.id, "alias": job.alias, "title": job.title, "kind": job.kind.value,
            "kind_label": KINDS[job.kind].label, "build_label": KINDS[job.kind].build_label,
            "recipe_id": job.recipe_id, "category_id": job.category_id,
            "category_label": cat.label if cat else None, "created_at": job.created_at, "source": job.source,
            "counts": agg["counts"], "by_stage": agg["by_stage"], "current_tab": agg["current_tab"],
            "waiting_on_user": agg["waiting_on_user"], "next_action": next_action(agg),
            "active_run": active_run_for(studio, ctx, job.id), "legacy": job.id.startswith("bat_"),
            "legacy_recipe": legacy_variant(snap) if snap else None, "archived_at": job.archived_at,
            "variant": job.variant, "direct": job.direct, "family": _family_ref(ctx, job),
            "batch": (batches if batches is not None else batch_index(ctx)).get(job.id),
            "rounds": len(items[0].candidate_sets) if len(items) == 1 else 0,
            "progress": progress(agg, len(items[0].candidate_sets) if items else 0, KINDS[job.kind].build_label)}


def list_jobs(studio: Studio, ctx: ProjectContext) -> list[dict[str, Any]]:
    batches = batch_index(ctx)
    out = [job_summary(studio, ctx, load_job(ctx.store, j)[0], batches) for j in job_ids(ctx)]
    return sorted(out, key=lambda b: b["created_at"], reverse=True)


def job_detail(studio: Studio, ctx: ProjectContext, job_id: str, build: dict[str, Any]) -> dict[str, Any]:
    """`build` is the live readiness of the recipe's build step (services.runtime.build_readiness)."""
    job, _ = load_job(ctx.store, job_id)
    recipe = RECIPES[job.recipe_id]
    available = build["state"] == "ready"
    items = load_items(ctx.store, job)
    snap = ctx.store.read_snapshot(items[0].snapshot_sha) if items else None
    return {
        **job_summary(studio, ctx, job),
        "seed_family": job.seed_family,
        "config_revision": job.config_revision,
        "recipe": {"id": recipe.id, "label": recipe.label, "build_label": KINDS[recipe.kind].build_label,
                   "build_available": available, "build_blocked_reason": build["reason"] if not available else "",
                   "build_state": build["state"],
                   "generation_available": recipe.generation is not None,
                   "generation_blocked_reason": recipe.generation_blocked_reason,
                   "stages": [s.__dict__ for s in recipe.stages]},
        "locked_template": snap["template"] if snap else recipe.template,
        "candidate_count": int((snap or {}).get("parameters", {}).get("candidate_count", 0)) or None,
        "style": _style_ref(snap),
        "variant_row": _variant_row(ctx, job),
        "items": [item_view(studio, ctx, job, it, available) for it in items],
    }


def _style_ref(snap: dict[str, Any] | None) -> dict[str, Any] | None:
    """The style recorded in the Job's frozen snapshot (not the live project config)."""
    style = (snap or {}).get("style")
    if not style:
        return None
    return {"name": style.get("name") or style.get("id"), "sha256": sha256_json(style)[:12]}


def _variant_row(ctx: ProjectContext, job: Job) -> dict[str, Any] | None:
    """The frozen plan row of a variant Job (its requested transform/change), readable before anything runs."""
    if not job.variant:
        return None
    from .variant_jobs import load_plan

    plan = load_plan(ctx, job.variant["plan_id"])
    rows = plan["rows"] if isinstance(plan, dict) else [r.model_dump(mode="json") for r in plan.rows]
    return next((r for r in rows if r["id"] == job.variant["row_id"]), None)
