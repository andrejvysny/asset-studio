"""Batch creation and read models. A single asset is a batch of one; nothing here is batch-wide state."""
from __future__ import annotations

from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.domain import Batch, BatchItem, BuildRun, ShotItem
from assetstudio_core.ids import new_id
from assetstudio_core.inheritance import ResolutionError, build_snapshot
from assetstudio_core.kinds import KINDS, Kind
from assetstudio_core.lifecycle import ACTIVE, aggregate, item_stage, next_action
from assetstudio_core.recipes import RECIPES
from assetstudio_core.seeds import new_seed_family
from assetstudio_storage.project import batch_key
from pydantic import BaseModel, Field

from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from .records import item_key, load_batch, load_build, load_cset, load_decision, load_items, load_prompt, load_qa


class NewItem(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    brief: str = Field(default="", max_length=4000)
    category_id: str | None = None
    kind: Kind | None = None
    shot_id: str | None = None
    target_asset_id: str | None = None


class CreateBatch(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    category_id: str | None = None
    kind: Kind | None = None
    items: list[NewItem] = Field(min_length=1, max_length=200)
    candidate_count: int | None = Field(default=None, ge=1, le=8)
    seed_family: int | None = Field(default=None, ge=0, le=2**53 - 1)
    source: str = Field(default="ad hoc", max_length=120)
    enhance: bool = True
    idempotency_key: str = Field(min_length=8, max_length=100)


def active_shot_claims(ctx: ProjectContext) -> dict[str, tuple[str, str]]:
    """shot id -> (batch id, item id) for unpublished, uncancelled items."""
    claims: dict[str, tuple[str, str]] = {}
    for batch_id in _batch_ids(ctx):
        batch, _ = load_batch(ctx.store, batch_id)
        for item in load_items(ctx.store, batch):
            if item.shot_id and not item.cancelled and item.published is None:
                claims[item.shot_id] = (batch.id, item.id)
    return claims


def _batch_ids(ctx: ProjectContext) -> list[str]:
    ids: list[str] = []
    cursor: str | None = None
    while True:
        keys, cursor = ctx.store.repo.list_keys("batches", cursor)
        ids += [k.split("/")[1] for k in keys if k.endswith("/batch.json")]
        if cursor is None:
            return ids


def list_batch_ids(ctx: ProjectContext) -> list[str]:
    return _batch_ids(ctx)


def create_batch(studio: Studio, ctx: ProjectContext, req: CreateBatch) -> tuple[Batch, bool]:
    ctx.require_writable()
    if (prior := studio.journal.command_result(req.idempotency_key, req.model_dump(mode="json"))) is not None:
        return load_batch(ctx.store, prior["batch_id"])[0], False
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
            snapshots.append(build_snapshot(cfg, cat, {"kind": it.kind or req.kind, "candidate_count":
                                                        req.candidate_count}))
        except ResolutionError as e:
            errors.append({"row": i, "name": it.name, "message": str(e)})
        if it.shot_id is not None and it.shot_id not in shots:
            errors.append({"row": i, "name": it.name, "message": f"unknown shot {it.shot_id}"})
    if errors:
        raise ApiError(422, "invalid_items", "some items cannot be planned", errors)
    recipes = {s["recipe"]["id"] for s in snapshots}
    if len(recipes) != 1:
        by = {r: [req.items[i].name for i, s in enumerate(snapshots) if s["recipe"]["id"] == r] for r in recipes}
        raise ApiError(422, "mixed_kinds", "a batch holds one output kind/recipe; split the selection", by)
    recipe = RECIPES[recipes.pop()]
    with ctx.store.lock:
        claims = active_shot_claims(ctx)
        taken = [{"shot_id": it.shot_id, "name": it.name, "batch_id": claims[it.shot_id][0]}
                 for it in req.items if it.shot_id in claims]
        if taken:
            raise ApiError(409, "shot_claimed", "some shot-list rows are already in an active batch", taken)
        now = now_iso()
        batch_id = new_id("bat")
        alias = f"b-{len(_batch_ids(ctx)) + 1:04d}"
        items: list[BatchItem] = []
        for it, snap in zip(req.items, snapshots, strict=True):
            ctx.store.save_snapshot(snap)
            shot: ShotItem | None = shots.get(it.shot_id or "")
            items.append(BatchItem(
                id=new_id("itm"), batch_id=batch_id, name=it.name, brief=it.brief or (shot.brief if shot else ""),
                category_id=snap["category_id"], shot_id=it.shot_id,
                target_asset_id=it.target_asset_id or (shot.target_asset_id if shot else None),
                snapshot_sha=snap["sha256"], created_at=now, updated_at=now))
        batch = Batch(id=batch_id, alias=alias, title=req.title, kind=recipe.kind, recipe_id=recipe.id,
                      category_id=req.category_id, created_at=now,
                      seed_family=req.seed_family if req.seed_family is not None else new_seed_family(),
                      item_ids=[i.id for i in items], source=req.source, config_revision=cfg.revision)
        for item in items:
            ctx.store.create(item_key(batch_id, item.id), item)
        ctx.store.create(batch_key(batch_id, "batch.json"), batch)
        studio.journal.record_command(req.idempotency_key, req.model_dump(mode="json"), {"batch_id": batch_id})
    studio.events.publish("batch", project_id=ctx.id, batch_id=batch_id)
    return batch, True


def _builds(ctx: ProjectContext, batch: Batch, items: list[BatchItem]) -> dict[str, BuildRun]:
    out = {}
    for it in items:
        if it.current_build:
            out[it.current_build] = load_build(ctx.store, batch.id, it.current_build)[0]
    return out


def batch_summary(ctx: ProjectContext, batch: Batch) -> dict[str, Any]:
    items = load_items(ctx.store, batch)
    agg = aggregate(items, _builds(ctx, batch, items))
    cat = ctx.config()[0].category(batch.category_id) if batch.category_id else None
    return {"id": batch.id, "alias": batch.alias, "title": batch.title, "kind": batch.kind.value,
            "kind_label": KINDS[batch.kind].label, "recipe_id": batch.recipe_id, "category_id": batch.category_id,
            "category_label": cat.label if cat else None, "created_at": batch.created_at, "source": batch.source,
            "counts": agg["counts"], "by_stage": agg["by_stage"], "current_tab": agg["current_tab"],
            "waiting_on_user": agg["waiting_on_user"], "next_action": next_action(agg)}


def list_batches(ctx: ProjectContext) -> list[dict[str, Any]]:
    out = [batch_summary(ctx, load_batch(ctx.store, b)[0]) for b in _batch_ids(ctx)]
    return sorted(out, key=lambda b: b["created_at"], reverse=True)


def _legal(item: BatchItem, stage_busy: bool, build: BuildRun | None, build_available: bool) -> dict[str, bool]:
    gen_busy = item.tasks.get("generate") is not None and item.tasks["generate"].state in ACTIVE
    open_prompt = item.current_set is None or item.regen_requested
    build_active = build is not None and build.status in ACTIVE
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
    }


def item_view(ctx: ProjectContext, batch: Batch, item: BatchItem, build_available: bool) -> dict[str, Any]:
    store = ctx.store
    build = load_build(store, batch.id, item.current_build)[0] if item.current_build else None
    st = item_stage(item, build)
    prompt = load_prompt(store, batch.id, item.current_prompt) if item.current_prompt else None
    cset = load_cset(store, batch.id, item.current_set) if item.current_set else None
    candidates = []
    if cset is not None:
        for c in cset.candidates:
            qa = load_qa(store, batch.id, item.qa[c.id]) if c.id in item.qa else None
            candidates.append({**c.model_dump(), "qa": None if qa is None else {
                "id": qa.id, "status": qa.policy["status"], "coverage": qa.policy["coverage"],
                "policy": qa.policy, "results": qa.results, "not_evaluated": qa.policy.get("not_evaluated")}})
    approval = load_decision(store, batch.id, item.approval) if item.approval else None
    return {
        **item.model_dump(mode="json", exclude={"tasks"}),
        "tasks": {k: v.model_dump() for k, v in item.tasks.items()},
        "stage": st.__dict__,
        "prompt": prompt.model_dump(mode="json") if prompt else None,
        "prompt_locked": item.current_set is not None and not item.regen_requested,
        "candidate_set": None if cset is None else {**cset.model_dump(mode="json", exclude={"candidates"}),
                                                    "candidates": candidates},
        "approval_detail": approval.model_dump(mode="json") if approval else None,
        "build": build.model_dump(mode="json") if build else None,
        "legal": _legal(item, st.busy, build, build_available),
    }


def batch_detail(ctx: ProjectContext, batch_id: str) -> dict[str, Any]:
    batch, _ = load_batch(ctx.store, batch_id)
    recipe = RECIPES[batch.recipe_id]
    items = load_items(ctx.store, batch)
    snap = ctx.store.read_snapshot(items[0].snapshot_sha) if items else None
    return {
        **batch_summary(ctx, batch),
        "seed_family": batch.seed_family,
        "config_revision": batch.config_revision,
        "recipe": {"id": recipe.id, "label": recipe.label, "build_label": KINDS[recipe.kind].build_label,
                   "build_available": recipe.build is not None, "build_blocked_reason": recipe.build_blocked_reason,
                   "generation_available": recipe.generation is not None,
                   "generation_blocked_reason": recipe.generation_blocked_reason,
                   "stages": [s.__dict__ for s in recipe.stages]},
        "locked_template": snap["template"] if snap else recipe.template,
        "items": [item_view(ctx, batch, it, recipe.build is not None) for it in items],
    }
