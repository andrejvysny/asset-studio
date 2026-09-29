"""Prompt stage: revisions, edits, enhancement, confirmation gate, regeneration.

Every function works on units ({job_id?, item_id, ...}) so the same code serves one Job and a cross-Job Batch
wave. Long work is created as StageTasks through the command envelope (durable intent, idempotent effects)."""
from __future__ import annotations

from typing import Any

from assetstudio_core.domain import JobItem, PromptRevision
from assetstudio_core.ids import derived_id, new_id
from assetstudio_core.recipes import RECIPES
from pydantic import BaseModel, Field

from ..coordinator.stages import STAGES, new_task, residency
from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from ..taskstore import Busy
from . import commands
from .promptrev import edited_bindings, make_revision
from .records import load_item, load_job, load_prompt, mutate_item, prompt_key
from .runs import active_run_for, record_wave
from .taskview import busy, item_tasks
from .variant_gen import variant_source
from .variants import EDIT_MODEL


class ItemRef(BaseModel):
    job_id: str | None = None  # required in cross-Job waves; implied by the URL for Job-level commands
    item_id: str
    expected_item_revision: int


class EditPrompt(ItemRef):
    description: str = Field(min_length=1, max_length=4000)


class EditPrompts(BaseModel):
    items: list[EditPrompt] = Field(min_length=1, max_length=500)


class EnhanceRequest(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=100)


class ConfirmItem(ItemRef):
    prompt_revision_id: str


class ConfirmAndGenerate(BaseModel):
    items: list[ConfirmItem] = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=100)


class RegenItem(ItemRef):
    description: str | None = Field(default=None, max_length=4000)


class Regenerate(BaseModel):
    items: list[RegenItem] = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=100)


class MarkRegenerate(BaseModel):
    items: list[ItemRef] = Field(min_length=1, max_length=500)
    mark: bool


def job_of(unit: ItemRef, job_id: str | None) -> str:
    jid = unit.job_id or job_id
    if jid is None:
        raise ApiError(422, "job_required", f"item {unit.item_id}: name its job_id")
    if job_id is not None and unit.job_id not in (None, job_id):
        raise ApiError(422, "job_mismatch", f"item {unit.item_id} names another Job")
    return jid


def prompt_open(item: JobItem, rounds: bool = True) -> bool:
    """Rounds: every candidate set is kept, so a prompt stays editable until a build is accepted. Legacy (v1
    batches API): the prompt locks once candidates exist, unless regeneration was requested."""
    if rounds:
        return item.accepted_build is None
    return item.current_set is None or item.regen_requested


def edit_bindings(ctx: ProjectContext, job_id: str, item: JobItem, refresh: bool = True) -> dict[str, Any]:
    vs = variant_source(ctx, load_job(ctx.store, job_id)[0])
    return edited_bindings(ctx.store, item, vs.bindings() if vs else None, refresh)


def check_instruction_fresh(ctx: ProjectContext, job_id: str, item: JobItem, prompt_id: str) -> None:
    """The prompt must have been written against the item's current references and the Job's frozen plan."""
    b = load_prompt(ctx.store, job_id, prompt_id).bindings
    if b.get("references_revision") is not None and b["references_revision"] != item.references_revision:
        raise ApiError(409, "stale_instruction_confirmation", "references changed; re-enhance or edit the prompt")
    job = load_job(ctx.store, job_id)[0]
    if job.variant and b.get("plan_sha256") and b["plan_sha256"] != job.variant.get("plan_sha256"):
        raise ApiError(409, "stale_instruction_confirmation", "the variant plan changed; re-enhance the prompt")


def outcome(studio: Studio, ctx: ProjectContext, job_id: str, item_id: str, fn: Any,
            expected: int | None) -> dict[str, Any]:
    try:
        item = mutate_item(studio, ctx, job_id, item_id, fn, expected)
        return {"job_id": job_id, "item_id": item_id, "ok": True, "revision": item.revision}
    except ApiError as e:
        return {"job_id": job_id, "item_id": item_id, "ok": False, "code": e.code, "message": e.message}


def edit_prompts(studio: Studio, ctx: ProjectContext, job_id: str | None, req: EditPrompts,
                 rounds: bool = True) -> list[dict[str, Any]]:
    results = []
    for e in req.items:
        jid = job_of(e, job_id)
        tasks = item_tasks(studio, ctx.id, load_item(ctx.store, jid, e.item_id)[0])

        def apply(item: JobItem, e: EditPrompt = e, tasks: Any = tasks, jid: str = jid) -> None:
            if not prompt_open(item, rounds):
                raise ApiError(409, "prompts_locked", "candidates exist: use Regenerate to change this prompt")
            if busy(tasks, "enhance", "generate"):
                raise ApiError(409, "busy", "enhancement or generation is running for this item")
            rev = make_revision(ctx.store, item, rid=new_id("prm"), origin="edited", description=e.description,
                                bindings=edit_bindings(ctx, jid, item))
            item.prompt_revisions.append(rev.id)
            item.current_prompt = rev.id
            item.prompt_confirmed = None
        results.append(outcome(studio, ctx, jid, e.item_id, apply, e.expected_item_revision))
    return results


# --- enhancement --------------------------------------------------------------------------------------------------
def plan_enhance(studio: Studio, ctx: ProjectContext, units: list[tuple[str, str]], run_id: str | None,
                 rounds: bool = True) -> dict[str, Any]:
    """units: (job_id, item_id). Items with candidates or active enhancement/generation are skipped with a reason
    (already confirmed unchanged prompts are never re-enhanced just because they join a run)."""
    eligible, skipped = [], []
    for jid, iid in units:
        item, _ = load_item(ctx.store, jid, iid)
        tasks = item_tasks(studio, ctx.id, item)
        if not prompt_open(item, rounds) or busy(tasks, "enhance", "generate"):
            skipped.append({"job_id": jid, "item_id": iid, "reason": "prompt locked or busy"})
        elif item.prompt_confirmed == item.current_prompt and item.prompt_confirmed is not None and not (
                rounds and item.current_set is not None):  # a new round may deliberately re-enhance
            skipped.append({"job_id": jid, "item_id": iid, "reason": "prompt already confirmed"})
        else:
            eligible.append({"job_id": jid, "item_id": iid, "from_prompt": item.current_prompt, "rounds": rounds,
                             "run_id": run_id or active_run_for(studio, ctx, jid)})
    return {"eligible": eligible, "skipped": skipped}


@commands.replayable("enhance")
def _enhance_effects(studio: Studio, ctx: ProjectContext, plan: dict[str, Any], cid: str) -> dict[str, Any]:
    new, skipped = [], list(plan["skipped"])
    for u in plan["eligible"]:
        new.append(new_task(studio, STAGES["enhance"], project_id=ctx.id, job_id=u["job_id"], item_id=u["item_id"],
                            input_key=f"{u['from_prompt']}|{cid}",
                            inputs={"from_prompt": u["from_prompt"], "rounds": u.get("rounds", True)},
                            run_id=u["run_id"], wave_id=plan.get("wave_id")))
    created = []
    for t in new:  # one at a time: a busy item is skipped, never the whole command
        try:
            created += studio.journal.tasks.create([t], cid)
        except Busy as e:
            skipped.append({"job_id": t.job_id, "item_id": t.item_id, "reason": str(e)})
    studio.events.publish("tasks", project_id=ctx.id)
    return {"command_id": cid, "tasks": created, "skipped": skipped}


def enqueue_enhance(studio: Studio, ctx: ProjectContext, job_id: str, req: EnhanceRequest,
                    rounds: bool = True) -> dict[str, Any]:
    body = {"job_id": job_id, **req.model_dump(mode="json")}

    def plan(cid: str) -> dict[str, Any]:
        p = plan_enhance(studio, ctx, [(job_id, i) for i in req.item_ids], None, rounds)
        if not p["eligible"]:
            raise ApiError(409, "nothing_eligible", "no selected item can be enhanced now", p["skipped"])
        return p
    return commands.execute(studio, ctx, "enhance", req.idempotency_key, body, plan)


# --- confirmation gate + generation -------------------------------------------------------------------------------
def generation_residency(studio: Studio, ctx: ProjectContext, item: JobItem) -> str:
    snap = ctx.store.read_snapshot(item.snapshot_sha)
    recipe = RECIPES[snap["recipe"]["id"]]
    if recipe.generation is None:
        raise ApiError(422, "generation_unavailable", f"{recipe.label}: {recipe.generation_blocked_reason}")
    if studio.engine is None:
        raise ApiError(503, "engine_unconfigured", "no image engine configured (library-only mode)")
    job = load_job(ctx.store, item.job_id)[0]
    edit = bool(job.variant) and not job.direct
    if edit and not studio.engine.supports("image_edit"):
        raise ApiError(422, "editing_model_unavailable", "the image engine cannot run source-conditioned edits")
    if not studio.engine.simulated:
        from .runtime import model_statuses

        statuses = model_statuses(studio)
        needed = [EDIT_MODEL] if edit else recipe.generation_models
        missing = [k for k in needed if not (k in statuses and statuses[k].ready)]
        if missing:
            raise ApiError(422, "missing_models", f"required models not installed: {', '.join(missing)}", missing)
    lora = (snap.get("values") or {}).get("style_lora")
    if lora:
        raise ApiError(422, "style_lora_unavailable", f"this item's configuration requires style LoRA "
                       f"{lora['model_id']}; style LoRAs are planned separately — fork the Job without it")
    if edit:
        return residency(studio, "generate", mode="image_edit")
    return residency(studio, "generate", speed_preset=snap["parameters"].get("speed_preset", "quality"))


def _confirm_units(studio: Studio, ctx: ProjectContext, job_id: str | None, items: list[ConfirmItem],
                   run_id: str | None, rounds: bool = True) -> dict[str, Any]:
    results, units = [], []
    for c in items:
        try:
            jid = job_of(c, job_id)
            item, _ = load_item(ctx.store, jid, c.item_id)
            if item.revision != c.expected_item_revision and item.prompt_confirmed != c.prompt_revision_id:
                raise ApiError(409, "stale_item", f"{item.name} changed (revision {item.revision}); reload")
            if not prompt_open(item, rounds):
                raise ApiError(409, "prompts_locked", "candidates exist for this prompt")
            if busy(item_tasks(studio, ctx.id, item), "enhance", "generate"):
                raise ApiError(409, "busy", "enhancement or generation is running")
            if item.current_prompt != c.prompt_revision_id:
                raise ApiError(409, "stale_prompt", "the prompt changed since you reviewed it; reload")
            check_instruction_fresh(ctx, jid, item, c.prompt_revision_id)
            res = generation_residency(studio, ctx, item)
        except ApiError as e:
            results.append({"job_id": c.job_id or job_id, "item_id": c.item_id, "ok": False, "code": e.code,
                            "message": e.message})
            continue
        units.append({"job_id": jid, "item_id": c.item_id, "prompt_revision_id": c.prompt_revision_id,
                      "residency": res, "run_id": run_id or active_run_for(studio, ctx, jid)})
        results.append({"job_id": jid, "item_id": c.item_id, "ok": True})
    return {"units": units, "results": results}


@commands.replayable("confirm_and_generate")
def _confirm_effects(studio: Studio, ctx: ProjectContext, plan: dict[str, Any], cid: str) -> dict[str, Any]:
    """Bind each exact prompt revision (idempotent), then create its generation task (idempotent by key)."""
    results = {(r["job_id"], r["item_id"]): r for r in plan["results"]}
    tasks = []
    for u in plan["units"]:
        def bind(x: JobItem, u: dict[str, Any] = u) -> None:
            if x.current_prompt != u["prompt_revision_id"]:
                raise ApiError(409, "stale_prompt", "the prompt changed before confirmation was applied")
            x.prompt_confirmed = u["prompt_revision_id"]
        r = outcome(studio, ctx, u["job_id"], u["item_id"], bind, None)
        if not r["ok"]:
            results[(u["job_id"], u["item_id"])] = r
            continue
        t = new_task(studio, STAGES["generate"], project_id=ctx.id, job_id=u["job_id"], item_id=u["item_id"],
                     input_key=u["prompt_revision_id"], inputs={"prompt_revision_id": u["prompt_revision_id"]},
                     run_id=u["run_id"], wave_id=plan.get("wave_id"), resident=u["residency"])
        try:
            tasks += studio.journal.tasks.create([t], cid)
        except Busy as e:
            results[(u["job_id"], u["item_id"])] = {**u, "ok": False, "code": "busy", "message": str(e)}
    record_wave(studio, ctx, plan, cid, "prompt_confirmation")
    studio.events.publish("tasks", project_id=ctx.id)
    return {"command_id": cid, "results": list(results.values()), "tasks": tasks,
            "operations": [{"id": tid, "kind": "generate"} for tid in tasks]}


def confirm_and_generate(studio: Studio, ctx: ProjectContext, job_id: str | None, req: ConfirmAndGenerate,
                         run_id: str | None = None, rounds: bool = True) -> dict[str, Any]:
    """Human gate: binds the exact prompt revision per item (across Jobs in a wave), then queues generation."""
    body = {"job_id": job_id, "run_id": run_id, **req.model_dump(mode="json")}

    def plan(cid: str) -> dict[str, Any]:
        p = _confirm_units(studio, ctx, job_id, req.items, run_id, rounds)
        return {**p, "run_id": run_id, "wave_id": derived_id("wav", cid) if run_id else None}
    return commands.execute(studio, ctx, "confirm_and_generate", req.idempotency_key, body, plan)


# --- regeneration ------------------------------------------------------------------------------------------------
def mark_regenerate(studio: Studio, ctx: ProjectContext, job_id: str | None,
                    req: MarkRegenerate) -> list[dict[str, Any]]:
    out = []
    for r in req.items:
        jid = job_of(r, job_id)
        tasks = item_tasks(studio, ctx.id, load_item(ctx.store, jid, r.item_id)[0])

        def apply(item: JobItem, tasks: Any = tasks) -> None:
            if item.current_set is None:
                raise ApiError(409, "no_candidates", "nothing to regenerate yet")
            if item.accepted_build is not None or busy(tasks, "build", "generate"):
                raise ApiError(409, "busy", "a build is running/accepted or generation is running")
            item.regen_requested = req.mark
            if req.mark:
                item.approval = None  # supersedes the current choice; the decision record stays in history
        out.append(outcome(studio, ctx, jid, r.item_id, apply, r.expected_item_revision))
    return out


def _regen_units(studio: Studio, ctx: ProjectContext, job_id: str | None, req: Regenerate) -> dict[str, Any]:
    results, units = [], []
    for r in req.items:
        try:
            jid = job_of(r, job_id)
            item, _ = load_item(ctx.store, jid, r.item_id)
            if item.revision != r.expected_item_revision:
                raise ApiError(409, "stale_item", f"{item.name} changed (revision {item.revision}); reload")
            if item.current_set is None:
                raise ApiError(409, "no_candidates", "nothing to regenerate yet")
            if item.accepted_build is not None or busy(item_tasks(studio, ctx.id, item), "build", "generate",
                                                       "enhance"):
                raise ApiError(409, "busy", "a build is running/accepted or generation is running")
            base = r.description or (load_prompt_desc(ctx, jid, item.current_prompt) if item.current_prompt else "")
            if not base:
                raise ApiError(409, "no_prompt", "no prompt to regenerate from")
            if not r.description and item.current_prompt:
                check_instruction_fresh(ctx, jid, item, item.current_prompt)  # same prompt: same bindings
            res = generation_residency(studio, ctx, item)
        except ApiError as e:
            results.append({"job_id": r.job_id or job_id, "item_id": r.item_id, "ok": False, "code": e.code,
                            "message": e.message})
            continue
        units.append({"job_id": jid, "item_id": r.item_id, "description": base, "residency": res,
                      "explicit": bool(r.description),
                      "run_id": active_run_for(studio, ctx, jid)})
        results.append({"job_id": jid, "item_id": r.item_id, "ok": True})
    return {"units": units, "results": results}


def load_prompt_desc(ctx: ProjectContext, job_id: str, rid: str) -> str:
    return ctx.store.get(prompt_key(job_id, rid), PromptRevision)[0].description


@commands.replayable("regenerate")
def _regen_effects(studio: Studio, ctx: ProjectContext, plan: dict[str, Any], cid: str) -> dict[str, Any]:
    """New prompt revision (confirmed by this submission; derived id so a replay reuses it) + a new candidate set."""
    results = {(r["job_id"], r["item_id"]): r for r in plan["results"]}
    tasks = []
    for u in plan["units"]:
        rid = derived_id("prm", cid, u["item_id"])

        def apply(item: JobItem, u: dict[str, Any] = u, rid: str = rid) -> None:
            rev = make_revision(ctx.store, item, rid=rid, origin="edited", description=u["description"],
                                bindings=edit_bindings(ctx, u["job_id"], item, refresh=u.get("explicit", True)))
            if rev.id not in item.prompt_revisions:
                item.prompt_revisions.append(rev.id)
            item.current_prompt = item.prompt_confirmed = rev.id
        r = outcome(studio, ctx, u["job_id"], u["item_id"], apply, None)
        if not r["ok"]:
            results[(u["job_id"], u["item_id"])] = r
            continue
        t = new_task(studio, STAGES["generate"], project_id=ctx.id, job_id=u["job_id"], item_id=u["item_id"],
                     input_key=rid, inputs={"prompt_revision_id": rid}, run_id=u["run_id"], resident=u["residency"])
        try:
            tasks += studio.journal.tasks.create([t], cid)
        except Busy as e:
            results[(u["job_id"], u["item_id"])] = {**u, "ok": False, "code": "busy", "message": str(e)}
    studio.events.publish("tasks", project_id=ctx.id)
    return {"command_id": cid, "results": list(results.values()), "tasks": tasks,
            "operations": [{"id": tid, "kind": "generate"} for tid in tasks]}


def regenerate(studio: Studio, ctx: ProjectContext, job_id: str | None, req: Regenerate) -> dict[str, Any]:
    body = {"job_id": job_id, **req.model_dump(mode="json")}
    return commands.execute(studio, ctx, "regenerate", req.idempotency_key, body,
                            lambda cid: _regen_units(studio, ctx, job_id, req))

