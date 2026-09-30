"""Batches (named groups of Jobs) and BatchRuns (frozen execution selections), plus standalone Job runs.

A Batch owns no item content. Saving or editing it never starts inference. A run is planned first (frozen,
hashed, previewable), then started idempotently; starting authorizes work only up to the first human gate. Later
gate actions are waves that bind exact then-current revisions across the run's Jobs.
"""
from __future__ import annotations

import json
from typing import Any

from assetstudio_core.canonical import now_iso, sha256_json
from assetstudio_core.domain import Batch, BatchRun, JobItem, WaveSelection
from assetstudio_core.ids import derived_id, is_job_id, new_id
from assetstudio_storage.project import batch_group_key
from assetstudio_storage.repo import NotFound
from pydantic import BaseModel, Field

from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from ..taskstore import TASK_ACTIVE
from . import commands
from .records import load_item, load_items, load_job

STOP_AT = ("prompt_review",)


def run_key(run_id: str) -> str:
    return f"runs/{run_id}.json"


def plan_key(plan_id: str) -> str:
    return f"runs/plans/{plan_id}.json"


def wave_key(wave_id: str) -> str:
    return f"waves/{wave_id}.json"


# --- Batch groups ------------------------------------------------------------------------------------------------
class CreateBatch(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    job_ids: list[str] = Field(default=[], max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=100)


class UpdateBatch(BaseModel):
    expected_revision: int
    title: str | None = Field(default=None, min_length=1, max_length=200)
    job_ids: list[str] | None = Field(default=None, max_length=200)


def _check_jobs(ctx: ProjectContext, job_ids: list[str]) -> list[str]:
    if len(set(job_ids)) != len(job_ids):
        raise ApiError(422, "duplicate_jobs", "a Job can appear only once in a Batch",
                       sorted({j for j in job_ids if job_ids.count(j) > 1}))
    for jid in job_ids:
        if not is_job_id(jid):
            raise ApiError(422, "invalid_job", f"{jid!r} is not a Job id")
        try:
            load_job(ctx.store, jid)
        except NotFound as e:
            raise ApiError(404, "unknown_job", f"Job {jid} is not in this project") from e
    return job_ids


def batch_ids(ctx: ProjectContext) -> list[str]:
    return [k.split("/")[1] for k in _keys(ctx, "execution_batches") if k.endswith("/batch.json")]


def _keys(ctx: ProjectContext, prefix: str) -> list[str]:
    out, cursor = [], None
    while True:
        keys, cursor = ctx.store.repo.list_keys(prefix, cursor)
        out += keys
        if cursor is None:
            return out


def load_batch_group(ctx: ProjectContext, batch_id: str) -> tuple[Batch, str]:
    try:
        return ctx.store.get(batch_group_key(batch_id, "batch.json"), Batch)
    except NotFound as e:
        raise ApiError(404, "unknown_batch", f"Batch {batch_id} does not exist") from e


def create_batch(studio: Studio, ctx: ProjectContext, req: CreateBatch) -> dict[str, Any]:
    """Save only: grouping Jobs never loads a model or enhances anything."""
    body = req.model_dump(mode="json")
    return commands.execute(studio, ctx, "batch_create", req.idempotency_key, body,
                            lambda cid: {"job_ids": _check_jobs(ctx, req.job_ids), "title": req.title,
                                         "batch_id": derived_id("bch", cid)})


def write_batch_group(ctx: ProjectContext, batch_id: str, title: str, job_ids: list[str]) -> Batch:
    """Idempotent save of a Batch group (derived id); an existing record is returned untouched."""
    key = batch_group_key(batch_id, "batch.json")
    with ctx.store.lock:
        existing, _ = ctx.store.get_opt(key, Batch)
        if existing is not None:
            return existing
        now = now_iso()
        batch = Batch(id=batch_id, alias=f"B-{len(batch_ids(ctx)) + 1:03d}", title=title, job_ids=job_ids,
                      created_at=now, updated_at=now)
        ctx.store.create(key, batch)
        return batch


@commands.replayable("batch_create")
def _batch_create(studio: Studio, ctx: ProjectContext, plan: dict[str, Any], cid: str) -> dict[str, Any]:
    batch = write_batch_group(ctx, plan["batch_id"], plan["title"], plan["job_ids"])
    studio.events.publish("batch", project_id=ctx.id, batch_id=batch.id)
    return {"batch_id": batch.id}


def update_batch(studio: Studio, ctx: ProjectContext, batch_id: str, req: UpdateBatch) -> dict[str, Any]:
    """Membership changes affect the NEXT run; a started run keeps its frozen selection."""
    ctx.require_writable()
    with ctx.store.lock:
        batch, token = load_batch_group(ctx, batch_id)
        if batch.revision != req.expected_revision:
            raise ApiError(409, "stale_batch", f"Batch changed (revision {batch.revision}); reload")
        if req.title is not None:
            batch.title = req.title
        if req.job_ids is not None:
            batch.job_ids = _check_jobs(ctx, req.job_ids)
        batch.revision += 1
        batch.updated_at = now_iso()
        ctx.store.replace(batch_group_key(batch_id, "batch.json"), batch, token)
    studio.events.publish("batch", project_id=ctx.id, batch_id=batch_id)
    return batch_summary(studio, ctx, batch)


# --- runs ----------------------------------------------------------------------------------------------------------
def runs(ctx: ProjectContext) -> list[BatchRun]:
    out = []
    for k in _keys(ctx, "runs"):
        if k.count("/") == 1 and k.endswith(".json"):
            out.append(ctx.store.get(k, BatchRun)[0])
    return sorted(out, key=lambda r: r.created_at)


def load_run(ctx: ProjectContext, run_id: str) -> tuple[BatchRun, str]:
    try:
        return ctx.store.get(run_key(run_id), BatchRun)
    except NotFound as e:
        raise ApiError(404, "unknown_run", f"run {run_id} does not exist") from e


def active_run_for(studio: Studio, ctx: ProjectContext, job_id: str) -> str | None:
    """The open run whose frozen selection includes this Job: Job-level actions become scoped continuations."""
    for r in runs(ctx):
        if (r.closed_at is None and job_id in r.selection
                and studio.journal.tasks.run_control(r.id) not in ("cancelled", "closed")):
            return r.id
    return None


def _classify(studio: Studio, ctx: ProjectContext, item: JobItem, direct: bool = False) -> tuple[str, str]:
    """(action, reason) for a run start that stops at prompt review."""
    from .taskview import busy, item_tasks

    tasks = item_tasks(studio, ctx.id, item)
    if item.cancelled:
        return "excluded", "item cancelled"
    if busy(tasks, "enhance", "generate", "qa", "build", "publish"):
        return "excluded", "work already in progress for this item"
    if item.published is not None and item.accepted_build is not None:
        return "done", "published"
    if direct:  # never enhanced or generated: waits for the human "run transform" gate
        return "at_gate", "direct transform (no model stages)"
    if item.current_set is not None and not item.regen_requested:
        return "at_gate", "candidates awaiting review or later stages"
    if item.prompt_confirmed is not None and item.prompt_confirmed == item.current_prompt:
        return "at_gate", "prompt confirmed; generate in a confirmation wave"
    if item.current_prompt is not None:
        return "at_gate", "prompt awaiting review (not re-enhanced)"
    return "enhance", "brief only"


class PlanRun(BaseModel):
    stop_at: str = Field(default="prompt_review", pattern="^prompt_review$")


def plan_run(studio: Studio, ctx: ProjectContext, batch_id: str | None, job_ids: list[str],
             stop_at: str = "prompt_review") -> dict[str, Any]:
    """Frozen, hashed preview of what a start would do. Stored immutably; nothing runs."""
    ctx.require_writable()
    batch_rev = None
    if batch_id is not None:
        batch, _ = load_batch_group(ctx, batch_id)
        job_ids, batch_rev = list(batch.job_ids), batch.revision
    if not job_ids:
        raise ApiError(422, "empty_batch", "add at least one Job before planning a run")
    from ..coordinator.stages import residency

    jobs, groups = [], {}
    enhance_res = residency(studio, "enhance")
    for jid in job_ids:
        job, _ = load_job(ctx.store, jid)
        owner = active_run_for(studio, ctx, jid)
        entries = []
        for item in load_items(ctx.store, job):
            action, reason = _classify(studio, ctx, item, job.direct)
            if owner is not None:
                action, reason = "excluded", f"managed by active run {owner}"
            entries.append({"item_id": item.id, "name": item.name, "revision": item.revision, "action": action,
                            "reason": reason, "snapshot_sha": item.snapshot_sha})
            if action == "enhance":
                groups[enhance_res] = groups.get(enhance_res, 0) + 1
        jobs.append({"job_id": jid, "title": job.title, "kind": job.kind.value, "recipe_id": job.recipe_id,
                     "revision": job.revision, "config_revision": job.config_revision, "items": entries})
    preflight = {"aux": "ready" if studio.execution.aux() is not None else "unavailable: no aux service configured",
                 "engine": "ready" if studio.execution.engine() is not None else "unavailable: library-only mode"}
    body = {"batch_id": batch_id, "batch_revision": batch_rev, "stop_at": stop_at, "jobs": jobs,
            "residency_groups": groups, "preflight": preflight}
    plan_id = new_id("sel")
    sha = sha256_json(body)
    ctx.store.create(plan_key(plan_id), {"plan_id": plan_id, "sha256": sha, "created_at": now_iso(), **body})
    return {"plan_id": plan_id, "plan_sha256": sha, **body, "counts": _plan_counts(jobs)}


def _plan_counts(jobs: list[dict[str, Any]]) -> dict[str, int]:
    items = [e for j in jobs for e in j["items"]]
    return {"jobs": len(jobs), "items": len(items), "enhance": sum(e["action"] == "enhance" for e in items),
            "at_gate": sum(e["action"] == "at_gate" for e in items),
            "excluded": sum(e["action"] == "excluded" for e in items),
            "done": sum(e["action"] == "done" for e in items)}


class StartRun(BaseModel):
    plan_id: str
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    batch_revision: int | None = None
    idempotency_key: str = Field(min_length=8, max_length=100)


def start_run(studio: Studio, ctx: ProjectContext, batch_id: str | None, req: StartRun) -> dict[str, Any]:
    """Idempotent: the same frozen plan always yields the same run (id derived from the plan)."""
    body = {"batch_id": batch_id, **req.model_dump(mode="json")}

    def plan(cid: str) -> dict[str, Any]:
        try:
            p = _read_plan(ctx, req.plan_id)
        except NotFound as e:
            raise ApiError(404, "unknown_plan", "plan not found; plan the run again") from e
        if p["sha256"] != req.plan_sha256 or p["batch_id"] != batch_id:
            raise ApiError(409, "plan_mismatch", "the plan hash or Batch does not match; review the plan again")
        if batch_id is not None:
            batch, _ = load_batch_group(ctx, batch_id)
            if batch.revision != p["batch_revision"] or req.batch_revision not in (None, batch.revision):
                raise ApiError(409, "stale_batch", "the Batch changed since it was planned; plan again")
        run_id = derived_id("brn", req.plan_id)
        for j in p["jobs"]:
            owner = active_run_for(studio, ctx, j["job_id"])
            if owner not in (None, run_id):
                raise ApiError(409, "job_in_active_run", f"Job {j['title']} is managed by run {owner}")
        return {"plan": p, "run_id": run_id}
    return commands.execute(studio, ctx, "run_start", req.idempotency_key, body, plan)


def _read_plan(ctx: ProjectContext, plan_id: str) -> dict[str, Any]:
    return json.loads(ctx.store.repo.read_object(plan_key(plan_id)).data)


@commands.replayable("run_start")
def _run_start(studio: Studio, ctx: ProjectContext, plan: dict[str, Any], cid: str) -> dict[str, Any]:
    from .prompts import _enhance_effects

    p, run_id = plan["plan"], plan["run_id"]
    if ctx.store.repo.stat_object(run_key(run_id)) is None:
        run = BatchRun(id=run_id, batch_id=p["batch_id"], batch_revision=p["batch_revision"], plan_id=p["plan_id"],
                       plan_sha256=p["sha256"], command_id=cid, stop_at=p["stop_at"],
                       selection={j["job_id"]: {e["item_id"]: e["revision"] for e in j["items"]
                                                if e["action"] != "excluded"} for j in p["jobs"]},
                       created_at=now_iso())
        ctx.store.create(run_key(run_id), run)
        if p["batch_id"] is not None:
            with ctx.store.lock:
                batch, token = load_batch_group(ctx, p["batch_id"])
                if run_id not in batch.runs:
                    batch.runs.append(run_id)
                    ctx.store.replace(batch_group_key(batch.id, "batch.json"), batch, token)
    eligible = []
    for j in p["jobs"]:
        for e in j["items"]:
            if e["action"] != "enhance":
                continue
            item, _ = load_item(ctx.store, j["job_id"], e["item_id"])
            if item.current_prompt is None and item.current_set is None:  # still eligible at start
                eligible.append({"job_id": j["job_id"], "item_id": e["item_id"], "from_prompt": None,
                                 "run_id": run_id})
    # Keyed by the run (not the command): starting the same plan again never enhances twice.
    res = _enhance_effects(studio, ctx, {"eligible": eligible, "skipped": []}, run_id)
    studio.events.publish("run", project_id=ctx.id, run_id=run_id, batch_id=p["batch_id"])
    return {"run_id": run_id, "tasks": res["tasks"], "skipped": res["skipped"]}


def run_task_ids(studio: Studio, ctx: ProjectContext, run_id: str, states: tuple[str, ...] = TASK_ACTIVE
                 ) -> list[str]:
    return [t.id for t in studio.journal.tasks.list(project_id=ctx.id, run_id=run_id, states=states)]


def control_run(studio: Studio, ctx: ProjectContext, run_id: str, action: str) -> dict[str, Any]:
    """pause: finish the current task, admit nothing further. resume: explicit. cancel: only this run's tasks
    (not a global interrupt, never data deletion). close: only when nothing is active; undecided items stay.
    The run-level intent is persisted FIRST so tasks created concurrently or after a restart obey it."""
    ctx.require_writable()
    run, token = load_run(ctx, run_id)
    tasks = studio.journal.tasks
    current = tasks.run_control(run_id)
    if action not in ("pause", "resume", "cancel", "close"):
        raise ApiError(400, "invalid_action", action)
    if run.closed_at is not None or current == "closed":
        raise ApiError(409, "invalid_transition", f"run is closed; cannot {action}")
    if action in ("pause", "resume") and current == "cancelled":
        raise ApiError(409, "invalid_transition", f"run is cancelled; cannot {action}")
    legacy_paused = any(t.control == "paused" for t in tasks.list(project_id=ctx.id, run_id=run_id,
                                                                  states=TASK_ACTIVE))
    if action == "resume" and current != "paused" and not legacy_paused:
        raise ApiError(409, "invalid_transition", "only a paused run can be resumed")
    n = 0
    if action == "pause":
        tasks.set_run_control(ctx.id, run_id, "paused")
        n = tasks.set_control(run_task_ids(studio, ctx, run_id), "paused", ("run",))
    elif action == "resume":
        tasks.set_run_control(ctx.id, run_id, "run")
        n = tasks.set_control(run_task_ids(studio, ctx, run_id), "run", ("paused",))
    elif action == "cancel":
        tasks.set_run_control(ctx.id, run_id, "cancelled")
        n = len(tasks.request_cancel(run_task_ids(studio, ctx, run_id)))
    elif run_task_ids(studio, ctx, run_id):
        raise ApiError(409, "run_active", "pause or cancel the run's active work before closing it")
    else:
        run.closed_at, run.close_reason = now_iso(), "closed by operator"
        ctx.store.replace(run_key(run_id), run, token)
        tasks.set_run_control(ctx.id, run_id, "closed")
    studio.events.publish("run", project_id=ctx.id, run_id=run_id)
    return {"run_id": run_id, "action": action, "affected": n, **run_summary(studio, ctx, load_run(ctx, run_id)[0])}


def require_wave(studio: Studio, ctx: ProjectContext, run_id: str, units: list[Any]) -> BatchRun:
    """Validate a wave BEFORE any effect: the run is open and every unit names an item frozen in its selection.
    A paused run still accepts waves (human decisions); the tasks they create are admitted paused."""
    run, _ = load_run(ctx, run_id)
    control = studio.journal.tasks.run_control(run_id)
    if run.closed_at is not None or control in ("cancelled", "closed"):
        raise ApiError(409, "run_not_open", f"run {run_id} is {'closed' if run.closed_at else control}")
    if any(u.job_id is None for u in units):
        raise ApiError(422, "job_required", "every wave item must name its job_id",
                       sorted({u.item_id for u in units if u.job_id is None}))
    foreign = sorted({u.item_id for u in units if u.item_id not in run.selection.get(u.job_id, {})})
    if foreign:
        raise ApiError(422, "not_in_run", "items are not part of this run's frozen selection", foreign)
    return run


def record_wave(studio: Studio, ctx: ProjectContext, plan: dict[str, Any], cid: str, gate: str) -> None:
    """Immutable record of a cross-Job gate action (units = exact bound revisions); linked from its run."""
    run_id, wave_id = plan.get("run_id"), plan.get("wave_id")
    if not run_id or not wave_id:
        return
    wave = WaveSelection(id=wave_id, run_id=run_id, gate=gate, command_id=cid,  # type: ignore[arg-type]
                         units=[{k: u.get(k) for k in ("job_id", "item_id", "prompt_revision_id", "approval_id",
                                                        "build_run_id") if u.get(k)} for u in plan["units"]],
                         created_at=now_iso())
    if ctx.store.repo.stat_object(wave_key(wave_id)) is None:
        ctx.store.create(wave_key(wave_id), wave)
    with ctx.store.lock:
        run, token = load_run(ctx, run_id)
        if wave_id not in run.waves:
            run.waves.append(wave_id)
            ctx.store.replace(run_key(run_id), run, token)


# --- read models -----------------------------------------------------------------------------------------------
def run_summary(studio: Studio, ctx: ProjectContext, run: BatchRun) -> dict[str, Any]:
    """Exact counts with explicit denominators; completion of a run never implies publication."""
    from .taskview import item_tasks

    counts = {"jobs": len(run.selection), "items": 0, "enhanced": 0, "enhance_failed": 0, "prompts_confirmed": 0,
              "prompts_waiting": 0, "candidates_ready": 0, "approved": 0, "undecided": 0, "builds_valid": 0,
              "builds_invalid": 0, "builds_failed": 0, "accepted": 0, "published": 0, "active_tasks": 0,
              "failed_tasks": 0, "paused_tasks": 0}
    run_tasks = studio.journal.tasks.list(project_id=ctx.id, run_id=run.id)
    counts["active_tasks"] = sum(t.state in TASK_ACTIVE for t in run_tasks)
    counts["failed_tasks"] = sum(t.state == "failed" for t in run_tasks)
    counts["paused_tasks"] = sum(t.control == "paused" and t.state in TASK_ACTIVE for t in run_tasks)
    waiting = 0
    for jid, items in run.selection.items():
        for iid in items:
            try:
                item, _ = load_item(ctx.store, jid, iid)
            except NotFound:
                continue
            counts["items"] += 1
            tasks = item_tasks(studio, ctx.id, item)
            enh = tasks.get("enhance")
            counts["enhanced"] += bool(enh and enh.state == "succeeded")
            counts["enhance_failed"] += bool(enh and enh.state == "failed")
            confirmed = item.prompt_confirmed is not None and item.prompt_confirmed == item.current_prompt
            counts["prompts_confirmed"] += confirmed
            counts["prompts_waiting"] += item.current_prompt is not None and not confirmed and item.current_set is None
            counts["candidates_ready"] += item.current_set is not None
            counts["approved"] += item.approval is not None
            counts["undecided"] += item.current_set is not None and item.approval is None
            counts["accepted"] += item.accepted_build is not None
            counts["published"] += item.published is not None
            b = tasks.get("build")
            counts["builds_failed"] += bool(b and b.state == "failed")
    for t in run_tasks:
        if t.stage in ("finalize", "derive") and t.state == "succeeded" and t.result:
            counts["builds_valid" if t.result.get("result") == "valid" else "builds_invalid"] += 1
    waiting = counts["prompts_waiting"] + counts["undecided"]
    control = studio.journal.tasks.run_control(run.id)
    if run.closed_at or control == "closed":
        status = "closed"
    elif control == "cancelled":
        status = "cancelled"
    elif control == "paused" or counts["paused_tasks"]:
        status = "paused"
    elif counts["active_tasks"]:
        status = "running"
    elif waiting or counts["approved"] > counts["accepted"]:
        status = "waiting_for_review"
    else:
        status = "completed_with_errors" if counts["failed_tasks"] else "completed"
    return {"id": run.id, "batch_id": run.batch_id, "plan_id": run.plan_id, "created_at": run.created_at,
            "closed_at": run.closed_at, "stop_at": run.stop_at, "status": status, "counts": counts,
            "control": control, "control_revision": studio.journal.tasks.run_control_revision(run.id),
            "waves": run.waves, "job_ids": list(run.selection)}


def batch_summary(studio: Studio, ctx: ProjectContext, batch: Batch) -> dict[str, Any]:
    kinds, items = set(), 0
    for jid in batch.job_ids:
        try:
            job, _ = load_job(ctx.store, jid)
        except NotFound:
            continue
        kinds.add(job.kind.value)
        items += len(job.item_ids)
    latest = None
    if batch.runs:
        try:
            latest = run_summary(studio, ctx, load_run(ctx, batch.runs[-1])[0])
        except ApiError:
            latest = None
    return {"id": batch.id, "alias": batch.alias, "title": batch.title, "job_ids": batch.job_ids,
            "kinds": sorted(kinds), "jobs": len(batch.job_ids), "items": items, "revision": batch.revision,
            "created_at": batch.created_at, "updated_at": batch.updated_at, "runs": batch.runs,
            "latest_run": latest}


def list_batches(studio: Studio, ctx: ProjectContext) -> list[dict[str, Any]]:
    out = [batch_summary(studio, ctx, load_batch_group(ctx, b)[0]) for b in batch_ids(ctx)]
    return sorted(out, key=lambda b: b["created_at"], reverse=True)
