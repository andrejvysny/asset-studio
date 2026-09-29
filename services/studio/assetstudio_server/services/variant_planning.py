"""Explicit, asynchronous VLM planning for variant drafts: source analysis and row suggestion.

Both are journal tasks on GPU1 (job_id = item_id = the draft id), created through replayable commands. Saving or
patching a draft never triggers them, and a suggestion is only ever copied into `rows` by apply_suggestion."""
from __future__ import annotations

from typing import Any, Literal

from assetstudio_core.ids import new_id
from assetstudio_core.variants import MAX_ROWS, Method, VariantDraft, VariantRow
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..coordinator.stages import STAGES, new_task
from ..coordinator.stages.base import model_identity
from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from ..taskstore import TASK_ACTIVE, Busy
from . import commands
from . import variant_analysis as va
from .variants import load_draft, save_draft

STAGE_KEYS = {"variant_analyze": "analyze", "variant_suggest": "suggest"}


class AnalyzeReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=100)


class SuggestReq(AnalyzeReq):
    count: int = Field(ge=1, le=MAX_ROWS)
    request: str | None = Field(default=None, max_length=4000)


class ApplyReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int
    mode: Literal["replace_empty", "append"]
    indices: list[int] | None = None


def _editable(ctx: ProjectContext, draft_id: str) -> VariantDraft:
    ctx.require_writable()
    draft = load_draft(ctx, draft_id)[0]
    if draft.materialized is not None:
        raise ApiError(409, "draft_materialized", "this draft was already saved as Jobs; start a new draft")
    return draft


def _active(studio: Studio, ctx: ProjectContext, draft_id: str) -> list[Any]:
    return [t for t in studio.journal.tasks.list(project_id=ctx.id, item_id=draft_id) if t.state in TASK_ACTIVE]


def _queue_plan(cid: str, draft_id: str, stage: str, inputs: dict[str, Any]) -> dict[str, Any]:
    return {"draft_id": draft_id, "stage": stage, "input_key": cid, "inputs": inputs}


@commands.replayable("variant_analyze")
def _queue_effects(studio: Studio, ctx: ProjectContext, plan: dict[str, Any], cid: str) -> dict[str, Any]:
    d = plan["draft_id"]
    task = new_task(studio, STAGES[plan["stage"]], project_id=ctx.id, job_id=d, item_id=d,
                    input_key=plan["input_key"], inputs=plan["inputs"])
    try:
        task_id = studio.journal.tasks.create([task], cid)[0]
    except Busy as e:  # lost a race with another planning command: join it rather than fail after the intent
        return {"task_id": e.owner, "joined": True}
    studio.events.publish("task", project_id=ctx.id, job_id=d, item_id=d, task_id=task_id)
    return {"task_id": task_id}


commands.REPLAY["variant_suggest"] = _queue_effects


def cached_analysis(studio: Studio, ctx: ProjectContext, draft: VariantDraft) -> dict[str, Any] | None:
    if draft.reference_set_id is None:
        return None
    aid = va.analysis_id_for(draft.reference_set_id, model_identity(studio, va.VLM_MODEL), draft.request)
    return va.read_analysis(ctx, aid)


def analyze_source(studio: Studio, ctx: ProjectContext, draft_id: str, req: AnalyzeReq) -> tuple[int, dict[str, Any]]:
    draft = _editable(ctx, draft_id)
    rec = cached_analysis(studio, ctx, draft)
    if rec is not None:
        if draft.analysis_id != rec["id"]:
            va.attach_analysis(ctx, draft_id, rec["id"])
        return 200, {"analysis_id": rec["id"], "analysis": rec, "task_id": None}
    for t in _active(studio, ctx, draft_id):
        if t.stage == "variant_analyze":
            return 202, {"task_id": t.id, "joined": True}
        raise ApiError(409, "plan_task_active", f"{STAGE_KEYS[t.stage]} is still running for this draft", t.id)
    body = {"draft_id": draft_id, **req.model_dump()}
    res = commands.execute(studio, ctx, "variant_analyze", req.idempotency_key, body,
                           lambda cid: _queue_plan(cid, draft_id, "variant_analyze", {"request": draft.request}))
    return 202, {"task_id": res["task_id"]}


def suggest_plan(studio: Studio, ctx: ProjectContext, draft_id: str, req: SuggestReq) -> dict[str, Any]:
    draft = _editable(ctx, draft_id)
    if draft.method is Method.direct_transform:
        raise ApiError(422, "unsupported_configuration", "row suggestions do not apply to direct transforms")
    active = _active(studio, ctx, draft_id)
    if active:
        raise ApiError(409, "plan_task_active", f"{STAGE_KEYS[active[0].stage]} is still running for this draft",
                       active[0].id)
    inputs = {"count": req.count, "intent": draft.intent.value if draft.intent else "related",
              "request": draft.request if req.request is None else req.request,
              "preserve": [c.text for c in draft.preserve]}
    body = {"draft_id": draft_id, **req.model_dump()}
    res = commands.execute(studio, ctx, "variant_suggest", req.idempotency_key, body,
                           lambda cid: _queue_plan(cid, draft_id, "variant_suggest", inputs))
    return {"task_id": res["task_id"]}


def apply_suggestion(ctx: ProjectContext, draft_id: str, req: ApplyReq) -> VariantDraft:
    ctx.require_writable()
    with ctx.store.lock:
        draft, token = load_draft(ctx, draft_id)
        if draft.materialized is not None:
            raise ApiError(409, "draft_materialized", "this draft was already saved as Jobs; start a new draft")
        if draft.revision != req.expected_revision:
            raise ApiError(409, "stale_variant_plan", f"draft changed (revision {draft.revision}); reload")
        suggested = (draft.suggestion or {}).get("rows") or []
        if not suggested:
            raise ApiError(409, "no_suggestion", "there is no suggestion to apply")
        if req.mode == "replace_empty" and draft.rows:
            raise ApiError(409, "rows_not_empty", "the draft already has rows; append instead")
        picked = _pick(suggested, req.indices)
        if len(draft.rows) + len(picked) > MAX_ROWS:
            raise ApiError(422, "too_many_rows", f"a draft holds at most {MAX_ROWS} rows")
        draft.rows = [*draft.rows, *_new_rows(picked, draft.candidates_per_row)]
        return save_draft(ctx, draft, token)


def _pick(suggested: list[dict[str, Any]], indices: list[int] | None) -> list[dict[str, Any]]:
    if indices is None:
        return suggested
    if len(set(indices)) != len(indices) or any(not 0 <= i < len(suggested) for i in indices):
        raise ApiError(422, "invalid_indices", f"indices must be unique and within 0..{len(suggested) - 1}")
    return [suggested[i] for i in indices]


def _new_rows(picked: list[dict[str, Any]], candidates: int) -> list[VariantRow]:
    try:
        return [VariantRow(id=new_id("row"), label=r["label"], change_request=r.get("change_request", ""),
                           candidate_count=candidates) for r in picked]
    except (ValidationError, KeyError) as e:
        raise ApiError(422, "invalid_row", f"suggested row cannot be applied: {e}") from e


def read_analysis(ctx: ProjectContext, draft_id: str) -> dict[str, Any]:
    draft = load_draft(ctx, draft_id)[0]
    rec = va.read_analysis(ctx, draft.analysis_id) if draft.analysis_id else None
    if rec is None:
        raise ApiError(404, "no_analysis", "this draft has no source analysis")
    return rec


def task_states(studio: Studio, project_id: str, draft_id: str) -> dict[str, dict[str, Any]]:
    """Latest planning task per kind from the journal (an active one wins over an older finished one)."""
    out: dict[str, dict[str, Any]] = {k: {"state": "idle", "error": None, "code": None, "task_id": None}
                                      for k in STAGE_KEYS.values()}
    latest: dict[str, Any] = {}
    for t in studio.journal.tasks.list(project_id=project_id, item_id=draft_id):
        cur = latest.get(t.stage)
        if t.stage in STAGE_KEYS and (cur is None or cur.state not in TASK_ACTIVE or t.state in TASK_ACTIVE):
            latest[t.stage] = t
    for stage, t in latest.items():
        err = t.error or {}
        out[STAGE_KEYS[stage]] = {"state": t.state, "error": err.get("message") if t.state != "succeeded" else None,
                                  "code": err.get("code") if t.state != "succeeded" else None, "task_id": t.id}
    return out
