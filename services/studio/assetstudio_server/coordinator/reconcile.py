"""Idempotent downstream scheduling and startup reconciliation (the outbox half of multi-step commands).

A committed stage result must always lead to its required downstream tasks, even if the process died between
the two writes: ensure_downstream is called on commit AND for every succeeded task whose downstream is not yet
marked done when the Studio starts.
"""
from __future__ import annotations

import logging
from typing import Any

from ..registry import ProjectContext
from ..services.records import load_item, load_job
from ..studio import Studio
from ..taskstore import Busy, RunNotOpen, StageTask
from .stages import STAGES, new_task
from .stages.qa import qa_needs
from .stages.qa_compare import compare_needed

log = logging.getLogger("assetstudio.reconcile")


def qa_tasks(studio: Studio, ctx: ProjectContext, t: StageTask, candidate_set_id: str) -> list[Any]:
    item, _ = load_item(ctx.store, t.job_id, t.item_id)
    snap = ctx.store.read_snapshot(item.snapshot_sha)
    needs_mask, needs_vlm = qa_needs(snap)
    common = {"project_id": t.project_id, "job_id": t.job_id, "item_id": t.item_id, "input_key": candidate_set_id,
              "run_id": t.run_id, "wave_id": t.wave_id}
    ins = {"candidate_set_id": candidate_set_id}
    out, deps = [], []
    if studio.aux is not None and needs_mask:
        m = new_task(studio, STAGES["mask"], inputs=ins, **common)
        out.append(m)
        deps.append(m.id)
    if studio.aux is not None and needs_vlm:
        v = new_task(studio, STAGES["qa_vlm"], inputs=ins, **common)
        out.append(v)
        deps.append(v.id)
    if studio.aux is not None and compare_needed(load_job(ctx.store, t.job_id)[0], item, snap, ctx.store):
        c = new_task(studio, STAGES["qa_compare"], inputs=ins, **common)
        out.append(c)
        deps.append(c.id)
    out.append(new_task(studio, STAGES["qa_finalize"], inputs={**ins, "soft_deps": True}, deps=deps, **common))
    return out


def downstream_for(studio: Studio, ctx: ProjectContext, t: StageTask, result: dict[str, Any] | None) -> list[Any]:
    """Tasks that must exist once `t` succeeded with `result` (idempotent by logical key)."""
    if t.stage == "generate" and result and result.get("candidate_set_id"):
        return qa_tasks(studio, ctx, t, result["candidate_set_id"])
    return []


def ensure_downstream(studio: Studio, ctx: ProjectContext, t: StageTask | None) -> None:
    """Repair path (startup): a succeeded task whose downstream was not recorded as created."""
    if t is None or t.state != "succeeded":
        return
    tasks = studio.journal.tasks
    new = downstream_for(studio, ctx, t, t.result)
    if new:
        try:
            tasks.create(new, t.command_id)
        except Busy as e:  # an older chain still owns the stage: retry_deferred admits it once that finishes
            log.info("downstream of %s deferred: %s", t.id, e)
            return
        except RunNotOpen as e:  # the run was cancelled/closed: the follow-up is intentionally not created
            log.info("downstream of %s skipped: %s", t.id, e)
    tasks.mark_downstream(t.id)


def retry_deferred(studio: Studio, project_id: str | None = None, item_id: str | None = None) -> None:
    """Admit follow-up chains that were deferred behind an older owner. Never raises: called after every task
    outcome and from the retry loop."""
    try:
        pending = studio.journal.tasks.pending_downstream(project_id, item_id)
    except Exception:
        log.exception("listing deferred downstream failed")
        return
    for t in pending:
        try:
            ensure_downstream(studio, studio.registry.get(t.project_id), t)
        except Exception:
            log.exception("deferred downstream of %s failed", t.id)


def reconcile_on_start(studio: Studio) -> None:
    """Finish multi-step commands whose intent committed but whose effects did not, then repair downstream."""
    from ..services.commands import replay_open_intents

    replay_open_intents(studio)
    for t in studio.journal.tasks.pending_downstream():
        try:
            ensure_downstream(studio, studio.registry.get(t.project_id), t)
        except Exception:
            log.exception("downstream reconciliation for %s failed", t.id)
    _retire_legacy_operations(studio)


def _retire_legacy_operations(studio: Studio) -> None:
    """Pre-milestone per-command operations are no longer dispatched. Any still active are blocked visibly (never
    silently dropped): the operator re-issues the action through the current commands."""
    for op in studio.journal.list(states=("held", "queued", "running", "reconciling", "cancel_requested")):
        studio.journal.finish(op.id, "blocked", error={
            "code": "legacy_operation", "retryable": False,
            "message": "created by an older AssetStudio version; re-issue the action (its durable results are kept)"})
