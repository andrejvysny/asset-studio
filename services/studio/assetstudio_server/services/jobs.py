"""Jobs: configured production workflows of one or more items (formerly "batches"). Creating a Job saves it;
inference starts only through an explicit run (standalone or as part of a Batch)."""
from __future__ import annotations

from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.domain import BuildRun, Job, JobItem, ShotItem
from assetstudio_core.ids import derived_id
from assetstudio_core.inheritance import ResolutionError, build_snapshot
from assetstudio_core.kinds import KINDS, Kind
from assetstudio_core.lifecycle import aggregate, item_stage, next_action
from assetstudio_core.recipes import RECIPES, legacy_variant
from assetstudio_storage.project import job_descriptor_key
from pydantic import BaseModel, Field

from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from . import commands
from .records import item_key, load_build, load_cset, load_decision, load_items, load_job, load_prompt, load_qa
from .taskview import as_json, busy, item_tasks


class NewItem(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    brief: str = Field(default="", max_length=4000)
    category_id: str | None = None
    kind: Kind | None = None
    shot_id: str | None = None
    target_asset_id: str | None = None


class CreateJob(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    category_id: str | None = None
    kind: Kind | None = None
    items: list[NewItem] = Field(min_length=1, max_length=200)
    candidate_count: int | None = Field(default=None, ge=1, le=8)
    seed_family: int | None = Field(default=None, ge=0, le=2**53 - 1)
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
        items.append({"id": derived_id("itm", cid, str(n)), "name": it.name,
                      "brief": it.brief or (shot.brief if shot else ""), "category_id": snap["category_id"],
                      "shot_id": it.shot_id, "target_asset_id": it.target_asset_id or (
                          shot.target_asset_id if shot else None), "snapshot": snap})
    return {"job_id": job_id, "recipe_id": recipes.pop(), "title": req.title, "category_id": req.category_id,
            "seed_family": req.seed_family if req.seed_family is not None else _seed(), "source": req.source,
            "config_revision": cfg.revision, "items": items}


def _seed() -> int:
    from assetstudio_core.seeds import new_seed_family

    return new_seed_family()


@commands.replayable("job_create")
def _create_effects(studio: Studio, ctx: ProjectContext, plan: dict[str, Any], cid: str) -> dict[str, Any]:
    """Save only. Every id is derived from the command: a replay writes the same Job, never a second one."""
    job_id = plan["job_id"]
    if ctx.store.repo.stat_object(job_descriptor_key(job_id)) is None:
        now = now_iso()
        with ctx.store.lock:
            for it in plan["items"]:
                snap = it["snapshot"]
                ctx.store.save_snapshot(snap)
                item = JobItem(id=it["id"], job_id=job_id, name=it["name"], brief=it["brief"],
                               category_id=it["category_id"], shot_id=it["shot_id"],
                               target_asset_id=it["target_asset_id"], snapshot_sha=snap["sha256"], created_at=now,
                               updated_at=now)
                if ctx.store.repo.stat_object(item_key(job_id, item.id)) is None:
                    ctx.store.create(item_key(job_id, item.id), item)
            recipe = RECIPES[plan["recipe_id"]]
            job = Job(id=job_id, alias=f"J-{len(job_ids(ctx)) + 1:04d}", title=plan["title"], kind=recipe.kind,
                      recipe_id=recipe.id, category_id=plan["category_id"], created_at=now,
                      seed_family=plan["seed_family"], item_ids=[i["id"] for i in plan["items"]],
                      source=plan["source"], config_revision=plan["config_revision"])
            ctx.store.create(job_descriptor_key(job_id), job)
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


def job_summary(studio: Studio, ctx: ProjectContext, job: Job) -> dict[str, Any]:
    from .runs import active_run_for

    items = load_items(ctx.store, job)
    tasks = {i.id: item_tasks(studio, ctx.id, i) for i in items}
    agg = aggregate(items, _builds(ctx, job, items), tasks)
    cat = ctx.config()[0].category(job.category_id) if job.category_id else None
    snap = ctx.store.read_snapshot(items[0].snapshot_sha) if items else None
    return {"id": job.id, "alias": job.alias, "title": job.title, "kind": job.kind.value,
            "kind_label": KINDS[job.kind].label, "recipe_id": job.recipe_id, "category_id": job.category_id,
            "category_label": cat.label if cat else None, "created_at": job.created_at, "source": job.source,
            "counts": agg["counts"], "by_stage": agg["by_stage"], "current_tab": agg["current_tab"],
            "waiting_on_user": agg["waiting_on_user"], "next_action": next_action(agg),
            "active_run": active_run_for(studio, ctx, job.id), "legacy": job.id.startswith("bat_"),
            "legacy_recipe": legacy_variant(snap) if snap else None, "archived_at": job.archived_at}


def list_jobs(studio: Studio, ctx: ProjectContext) -> list[dict[str, Any]]:
    out = [job_summary(studio, ctx, load_job(ctx.store, j)[0]) for j in job_ids(ctx)]
    return sorted(out, key=lambda b: b["created_at"], reverse=True)


def _legal(item: JobItem, tasks: dict[str, Any], stage_busy: bool, build: BuildRun | None,
           build_available: bool) -> dict[str, bool]:
    gen_busy = busy(tasks, "generate")
    open_prompt = item.current_set is None or item.regen_requested
    build_active = busy(tasks, "build")
    return {
        "edit_prompt": open_prompt and not stage_busy and item.current_prompt is not None,
        "enhance": open_prompt and not stage_busy,
        "confirm": open_prompt and not stage_busy and item.current_prompt is not None and not gen_busy,
        "approve": item.current_set is not None and not item.regen_requested and not build_active
        and item.accepted_build is None,
        "mark_regenerate": item.current_set is not None and not build_active and item.accepted_build is None
        and not gen_busy,
        "build": build_available and item.approval is not None and not item.regen_requested and not build_active
        and item.accepted_build is None and (build is None or build.result != "valid"),
        "accept": build is not None and build.result == "valid" and item.accepted_build is None,
        "publish": item.accepted_build is not None and not stage_busy,
        "retry_preview": build is not None and build.result == "valid" and build.preview == "failed"
        and not build_active,
    }


def item_view(studio: Studio, ctx: ProjectContext, job: Job, item: JobItem, build_available: bool) -> dict[str, Any]:
    store = ctx.store
    build = load_build(store, job.id, item.current_build)[0] if item.current_build else None
    tasks = item_tasks(studio, ctx.id, item)
    st = item_stage(item, build, tasks)
    prompt = load_prompt(store, job.id, item.current_prompt) if item.current_prompt else None
    cset = load_cset(store, job.id, item.current_set) if item.current_set else None
    candidates = []
    if cset is not None:
        for c in cset.candidates:
            qa = load_qa(store, job.id, item.qa[c.id]) if c.id in item.qa else None
            candidates.append({**c.model_dump(), "qa": None if qa is None else {
                "id": qa.id, "status": qa.policy["status"], "coverage": qa.policy["coverage"],
                "policy": qa.policy, "results": qa.results, "not_evaluated": qa.policy.get("not_evaluated")}})
    approval = load_decision(store, job.id, item.approval) if item.approval else None
    history = []
    for rid in item.build_runs:
        r = load_build(store, job.id, rid)[0]
        history.append({"id": r.id, "status": r.status, "result": r.result, "kind": r.kind, "error": r.error,
                        "derived_from": r.derived_from, "created_at": r.created_at, "preview": r.preview,
                        "checkpoints": sorted(r.checkpoints), "has_raw": "raw" in r.artifacts,
                        "accepted": r.id == item.accepted_build, "current": r.id == item.current_build})
    return {
        **item.model_dump(mode="json", exclude={"tasks"}),
        "batch_id": job.id,  # v1 compatibility: the old API called the Job a batch
        "tasks": as_json(tasks),
        "stage": st.__dict__,
        "prompt": prompt.model_dump(mode="json") if prompt else None,
        "prompt_locked": item.current_set is not None and not item.regen_requested,
        "candidate_set": None if cset is None else {**cset.model_dump(mode="json", exclude={"candidates"}),
                                                    "candidates": candidates},
        "approval_detail": approval.model_dump(mode="json") if approval else None,
        "build": build.model_dump(mode="json") if build else None,
        "build_history": history,
        "legal": _legal(item, tasks, st.busy, build, build_available),
    }


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
        "items": [item_view(studio, ctx, job, it, available) for it in items],
    }
