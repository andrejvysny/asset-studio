"""Prompt stage: revisions, edits, enhancement passes, confirmation gate, regeneration."""
from __future__ import annotations

from typing import Any

from assetstudio_core.canonical import now_iso, sha256_json
from assetstudio_core.domain import JobItem, PromptRevision
from assetstudio_core.ids import new_id
from assetstudio_core.lifecycle import ACTIVE
from assetstudio_core.recipes import RECIPES
from assetstudio_storage.project import ProjectStore
from pydantic import BaseModel, Field

from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from .records import cmd_payload, load_item, load_job, mutate_item, prompt_key, set_task


class ItemRef(BaseModel):
    item_id: str
    expected_item_revision: int


class EditPrompt(ItemRef):
    description: str = Field(min_length=1, max_length=4000)


class EditPrompts(BaseModel):
    items: list[EditPrompt] = Field(min_length=1, max_length=200)


class EnhanceRequest(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=100)


class ConfirmItem(ItemRef):
    prompt_revision_id: str


class ConfirmAndGenerate(BaseModel):
    items: list[ConfirmItem] = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=100)


class RegenItem(ItemRef):
    description: str | None = Field(default=None, max_length=4000)


class Regenerate(BaseModel):
    items: list[RegenItem] = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=100)


class MarkRegenerate(BaseModel):
    items: list[ItemRef] = Field(min_length=1, max_length=200)
    mark: bool


def compose(description: str, suffix: str) -> str:
    desc = description.strip().rstrip(" .,")
    return f"{desc}, {suffix}" if suffix.strip() else desc


def make_revision(store: ProjectStore, item: JobItem, *, rid: str, origin: str, description: str,
                  enhancer: dict[str, Any] | None = None) -> PromptRevision:
    """Immutable prompt revision. Existing id (retry) returns the stored record unchanged."""
    key = prompt_key(item.job_id, rid)
    existing, _ = store.get_opt(key, PromptRevision)
    if existing is not None:
        return existing
    snap = store.read_snapshot(item.snapshot_sha)
    style = snap.get("style") or {}
    negative = ", ".join(x for x in (snap.get("negative", ""), style.get("negative", "")) if x)
    rev = PromptRevision(
        id=rid, item_id=item.id, number=len(item.prompt_revisions) + 1, parent_id=item.current_prompt,
        created_at=now_iso(), origin=origin, original_brief=item.brief, enhancer=enhancer,  # type: ignore[arg-type]
        description=description.strip(), template=snap["template"], positive=compose(description, snap["template"]),
        negative=negative, style_sha=sha256_json(style) if style else None, snapshot_sha=item.snapshot_sha)
    store.create(key, rev)
    return rev


def _prompt_open(item: JobItem) -> bool:
    return item.current_set is None or item.regen_requested


def _busy(item: JobItem, *stages: str) -> bool:
    return any(item.tasks.get(s) is not None and item.tasks[s].state in ACTIVE for s in stages)


def edit_prompts(studio: Studio, ctx: ProjectContext, batch_id: str, req: EditPrompts) -> list[dict[str, Any]]:
    results = []
    for e in req.items:
        def apply(item: JobItem, e: EditPrompt = e) -> None:
            if not _prompt_open(item):
                raise ApiError(409, "prompts_locked", "candidates exist: use Regenerate to change this prompt")
            if _busy(item, "enhance", "generate"):
                raise ApiError(409, "busy", "enhancement or generation is running for this item")
            rev = make_revision(ctx.store, item, rid=new_id("prm"), origin="edited", description=e.description)
            item.prompt_revisions.append(rev.id)
            item.current_prompt = rev.id
            item.prompt_confirmed = None
        results.append(_outcome(studio, ctx, batch_id, e.item_id, apply, e.expected_item_revision))
    return results


def _outcome(studio: Studio, ctx: ProjectContext, batch_id: str, item_id: str, fn: Any,
             expected: int | None) -> dict[str, Any]:
    try:
        item = mutate_item(studio, ctx, batch_id, item_id, fn, expected)
        return {"item_id": item_id, "ok": True, "revision": item.revision}
    except ApiError as e:
        return {"item_id": item_id, "ok": False, "code": e.code, "message": e.message}


def enqueue_enhance(studio: Studio, ctx: ProjectContext, batch_id: str, req: EnhanceRequest) -> dict[str, Any]:
    ctx.require_writable()
    load_job(ctx.store, batch_id)
    eligible, skipped = [], []
    for iid in req.item_ids:
        item, _ = load_item(ctx.store, batch_id, iid)
        if not _prompt_open(item) or _busy(item, "enhance", "generate"):
            skipped.append({"item_id": iid, "reason": "prompt locked or busy"})
        else:
            eligible.append(iid)
    if not eligible:
        raise ApiError(409, "nothing_eligible", "no selected item can be enhanced now", skipped)
    op, created = studio.journal.enqueue(project_id=ctx.id, batch_id=batch_id, kind="enhance", lane="gpu1",
                                         affinity="aux.text", payload={"batch_id": batch_id, "item_ids": eligible},
                                         idempotency_key=req.idempotency_key, hold=True)
    if created:
        for iid in eligible:
            mutate_item(studio, ctx, batch_id, iid, lambda it: set_task(it, "enhance", op.id, "queued"))
        studio.journal.release(op.id)
    return {"operation": op.public(), "skipped": skipped}


def _generation_affinity(studio: Studio, ctx: ProjectContext, item: JobItem) -> tuple[str, dict[str, Any]]:
    snap = ctx.store.read_snapshot(item.snapshot_sha)
    recipe = RECIPES[snap["recipe"]["id"]]
    if recipe.generation is None:
        raise ApiError(422, "generation_unavailable", f"{recipe.label}: {recipe.generation_blocked_reason}")
    if studio.engine is None:
        raise ApiError(503, "engine_unconfigured", "no image engine configured (library-only mode)")
    if not studio.engine.simulated:
        from .runtime import model_statuses

        statuses = model_statuses(studio)
        missing = [k for k in recipe.generation_models if not (k in statuses and statuses[k].ready)]
        if missing:
            raise ApiError(422, "missing_models", f"required models not installed: {', '.join(missing)}", missing)
    lora = snap["values"].get("style_lora")
    speed = snap["parameters"].get("speed_preset", "quality")
    return f"{recipe.generation}|lora={lora and lora['model_id']}:{lora and lora['strength']}|speed={speed}", snap


def confirm_and_generate(studio: Studio, ctx: ProjectContext, batch_id: str,
                         req: ConfirmAndGenerate) -> dict[str, Any]:
    """Human gate: binds the exact prompt revision per item, then durably queues candidate generation."""
    ctx.require_writable()
    prior = studio.journal.command_result(ctx.id, "confirm_and_generate", req.idempotency_key,
                                                 cmd_payload(req, batch_id))
    if prior is not None:
        return prior
    results, groups = [], {}
    for c in req.items:
        def apply(item: JobItem, c: ConfirmItem = c) -> None:
            if not _prompt_open(item):
                raise ApiError(409, "prompts_locked", "candidates exist for this prompt")
            if _busy(item, "enhance", "generate"):
                raise ApiError(409, "busy", "enhancement or generation is running")
            if item.current_prompt != c.prompt_revision_id:
                raise ApiError(409, "stale_prompt", "the prompt changed since you reviewed it; reload")
            item.prompt_confirmed = c.prompt_revision_id
        try:
            item, _ = load_item(ctx.store, batch_id, c.item_id)
            affinity, _ = _generation_affinity(studio, ctx, item)
        except ApiError as e:
            results.append({"item_id": c.item_id, "ok": False, "code": e.code, "message": e.message})
            continue
        out = _outcome(studio, ctx, batch_id, c.item_id, apply, c.expected_item_revision)
        results.append(out)
        if out["ok"]:
            groups.setdefault(affinity, []).append({"item_id": c.item_id, "prompt_revision_id": c.prompt_revision_id})
    ops = [_enqueue_generate(studio, ctx, batch_id, aff, items, f"{req.idempotency_key}:{i}")
           for i, (aff, items) in enumerate(sorted(groups.items()))]
    response = {"results": results, "operations": ops}
    studio.journal.record_command(ctx.id, "confirm_and_generate", req.idempotency_key, cmd_payload(req, batch_id),
                                  response)
    return response


def _enqueue_generate(studio: Studio, ctx: ProjectContext, batch_id: str, affinity: str,
                      items: list[dict[str, str]], key: str) -> dict[str, Any]:
    op, created = studio.journal.enqueue(project_id=ctx.id, batch_id=batch_id, kind="generate", lane="gpu0",
                                         affinity=affinity, payload={"batch_id": batch_id, "items": items},
                                         idempotency_key=key, hold=True)
    if created:
        for it in items:
            mutate_item(studio, ctx, batch_id, it["item_id"], lambda x: set_task(x, "generate", op.id, "queued"))
        studio.journal.release(op.id)
    return op.public()


def mark_regenerate(studio: Studio, ctx: ProjectContext, batch_id: str, req: MarkRegenerate) -> list[dict[str, Any]]:
    out = []
    for r in req.items:
        def apply(item: JobItem) -> None:
            if item.current_set is None:
                raise ApiError(409, "no_candidates", "nothing to regenerate yet")
            if item.accepted_build is not None or _busy(item, "build", "generate"):
                raise ApiError(409, "busy", "a build is running/accepted or generation is running")
            item.regen_requested = req.mark
            if req.mark:
                item.approval = None  # supersedes the current choice; the decision record stays in history
        out.append(_outcome(studio, ctx, batch_id, r.item_id, apply, r.expected_item_revision))
    return out


def regenerate(studio: Studio, ctx: ProjectContext, batch_id: str, req: Regenerate) -> dict[str, Any]:
    """New prompt revision (confirmed by this submission) + new candidate set for the selected rows only."""
    ctx.require_writable()
    prior = studio.journal.command_result(ctx.id, "regenerate", req.idempotency_key,
                                                 cmd_payload(req, batch_id))
    if prior is not None:
        return prior
    results, groups = [], {}
    for r in req.items:
        holder: dict[str, str] = {}

        def apply(item: JobItem, r: RegenItem = r, holder: dict[str, str] = holder) -> None:
            if item.current_set is None:
                raise ApiError(409, "no_candidates", "nothing to regenerate yet")
            if item.accepted_build is not None or _busy(item, "build", "generate", "enhance"):
                raise ApiError(409, "busy", "a build is running/accepted or generation is running")
            base = r.description or (ctx.store.get(prompt_key(batch_id, item.current_prompt or ""),
                                                   PromptRevision)[0].description if item.current_prompt else "")
            if not base:
                raise ApiError(409, "no_prompt", "no prompt to regenerate from")
            rev = make_revision(ctx.store, item, rid=new_id("prm"), origin="edited", description=base)
            item.prompt_revisions.append(rev.id)
            item.current_prompt = item.prompt_confirmed = rev.id
            item.regen_requested = True
            item.approval = None
            holder["rev"] = rev.id
        try:
            affinity, _ = _generation_affinity(studio, ctx, load_item(ctx.store, batch_id, r.item_id)[0])
        except ApiError as e:
            results.append({"item_id": r.item_id, "ok": False, "code": e.code, "message": e.message})
            continue
        out = _outcome(studio, ctx, batch_id, r.item_id, apply, r.expected_item_revision)
        results.append(out)
        if out["ok"]:
            groups.setdefault(affinity, []).append({"item_id": r.item_id, "prompt_revision_id": holder["rev"]})
    ops = [_enqueue_generate(studio, ctx, batch_id, aff, items, f"{req.idempotency_key}:{i}")
           for i, (aff, items) in enumerate(sorted(groups.items()))]
    response = {"results": results, "operations": ops}
    studio.journal.record_command(ctx.id, "regenerate", req.idempotency_key, cmd_payload(req, batch_id),
                                  response)
    return response
