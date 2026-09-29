"""Stage-first, model-aware scheduler.

Lanes: gpu0 (image engine), gpu1 (aux VLM/segmentation + 3D worker, one owner at a time), cpu (bounded pool).
Each lane repeatedly picks a residency group of ready StageTasks ACROSS all Jobs/Batches and executes it as one
bounded ModelPass under one resource grant, so a model stays resident while compatible work exists anywhere.
Job boundaries never cause an unload; fairness boundaries fall between tasks, never inside one.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from assetstudio_core.ids import is_id

from ..registry import ProjectContext
from ..studio import Studio
from ..taskstore import StageTask
from .errors import Blocked, Cancelled, ItemFailed, classify

log = logging.getLogger("assetstudio.coordinator")
LANES = ("gpu0", "gpu1", "cpu")
RETRY_BLOCKED_S = 20.0
BACKOFF_SLACK_S = 0.01  # wall-clock ms truncation + wall/monotonic conversion jitter
# Automatic retries of transiently blocked work are bounded (no hidden infinite retry).
MAX_AUTO_ATTEMPTS = 6
MAX_AUTO_RETRY_S = 2 * 3600.0

__all__ = ["Blocked", "Cancelled", "Coordinator", "ItemFailed", "Limits", "TaskEnv"]


@dataclass(frozen=True)
class Limits:
    """Conservative defaults (docs/architecture.md). Correctness never depends on them."""

    max_tasks_per_pass: int = 64
    max_pass_wall_s: float = 1800.0
    max_consecutive_passes: int = 4  # same residency in a row while other groups wait
    coalesce_window: int = 8  # mask/VLM tasks wait for a window of this size ...
    coalesce_max_wait_s: float = 45.0  # ... or this long, or until no producer is pending
    cpu_workers: int = 2


@dataclass
class TaskEnv:
    studio: Studio
    ctx: ProjectContext
    task: StageTask

    def progress(self, **fields: Any) -> None:
        self.task.progress.update(fields)
        self.studio.journal.tasks.progress(self.task.id, self.task.progress)
        self.studio.events.publish("task", project_id=self.ctx.id, job_id=self.task.job_id,
                                   item_id=self.task.item_id, run_id=self.task.run_id, task_id=self.task.id)

    def check_cancel(self) -> None:
        cur = self.studio.journal.tasks.get(self.task.id)
        if cur is not None and cur.control == "cancel_requested":
            raise Cancelled()

    def check_cancel_or(self, on_cancel: Callable[[], Any]) -> None:
        """Cancellation hook for engine work this task exclusively owns (e.g. its own prompt id)."""
        try:
            self.check_cancel()
        except Cancelled:
            on_cancel()
            raise

    def epoch(self, worker: str) -> int:
        """Lease epoch for a GPU1 worker (acquires the device if another worker holds it)."""
        return self.studio.lanes["gpu1"].acquire(worker)


def _age_s(iso: str) -> float:
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds()
    except ValueError:
        return 0.0


class Coordinator:
    def __init__(self, studio: Studio, stages: dict[str, Any] | None = None, limits: Limits | None = None) -> None:
        from .stages import STAGES

        self.studio = studio
        self.stages = stages if stages is not None else STAGES
        self.limits = limits or Limits()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last: dict[str, tuple[str | None, int]] = {}
        self._blocked_until: dict[str, float] = {}
        self._current: dict[str, dict[str, Any]] = {}
        self._pick_lock = threading.Lock()

    # --- lifecycle ------------------------------------------------------------------------------------------
    def _cancel_orphaned_prompts(self, orphans: list[dict[str, Any]]) -> None:
        """Cancelled-while-down generation tasks: stop their own unfinished engine prompts by exact id."""
        engine = self.studio.engine
        if engine is None:
            return
        for t in orphans:
            if t["stage"] != "generate":
                continue
            for slot in (t["progress"].get("engine") or {}).values():
                if isinstance(slot, dict) and slot.get("prompt_id") and "artifact_id" not in slot:
                    try:
                        engine.cancel(slot["prompt_id"])
                    except Exception as e:  # noqa: BLE001 - unknown outcome: logged, the prompt id stays recorded
                        log.warning("cancel of orphaned prompt %s (task %s) not confirmed: %s",
                                    slot["prompt_id"], t["id"], e)

    def start(self) -> None:
        rec = self.studio.journal.tasks.recover_after_restart()
        orphans = rec.pop("orphans", [])
        if rec["requeued"] or rec["cancelled"]:
            log.warning("restart reconciliation: %s", rec)
        self._cancel_orphaned_prompts(orphans)
        from .reconcile import reconcile_on_start

        reconcile_on_start(self.studio)
        workers = [("gpu0", 0), ("gpu1", 0)] + [("cpu", i) for i in range(self.limits.cpu_workers)]
        for lane, i in workers:
            t = threading.Thread(target=self._loop, args=(lane,), name=f"lane-{lane}-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        t = threading.Thread(target=self._retry_loop, name="retry-blocked", daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        with self.studio.journal.changed:
            self.studio.journal.changed.notify_all()
        for t in self._threads:
            t.join(timeout=5)

    def _loop(self, lane: str) -> None:
        while not self._stop.is_set():
            try:
                picked = self.choose(lane)
                if picked is None:
                    with self.studio.journal.changed:
                        self.studio.journal.changed.wait(timeout=1.0)
                    continue
                self.run_pass(lane, *picked)
            except Exception:  # supervision: a scheduler bug is logged; the lane keeps running
                log.exception("lane %s loop error", lane)
                time.sleep(1.0)

    # --- planning -------------------------------------------------------------------------------------------
    def _producers_pending(self, stage: str) -> bool:
        upstream = {"mask": "generate", "qa_vlm": "generate", "qa_compare": "generate"}.get(stage)
        if upstream is None:
            return False
        return any(t.stage == upstream for t in self.studio.journal.tasks.list(states=("queued", "running")))

    def _backing_off(self, residency: str, group: list[StageTask]) -> bool:
        """A blocked resource is not retried automatically for RETRY_BLOCKED_S; a task queued (e.g. explicitly
        retried) after the block started bypasses the backoff."""
        until = self._blocked_until.get(residency)
        if until is None or until <= time.monotonic():
            return False
        since = until - RETRY_BLOCKED_S - BACKOFF_SLACK_S
        return all(time.monotonic() - _age_s(t.updated_at) <= since for t in group)

    def _eligible(self, residency: str, group: list[StageTask]) -> bool:
        if self._backing_off(residency, group):
            return False
        stage = self.stages.get(group[0].stage)
        if stage is None or not getattr(stage, "coalesce", False):
            return True
        oldest = max(_age_s(t.updated_at) for t in group)
        return (len(group) >= self.limits.coalesce_window or oldest >= self.limits.coalesce_max_wait_s
                or not self._producers_pending(group[0].stage))

    def choose(self, lane: str) -> tuple[str, list[StageTask]] | None:
        """Pick the residency group to run next on `lane`: stay on the resident model while it has work (bounded
        by max_consecutive_passes), otherwise the group holding the highest-priority / oldest ready task."""
        with self._pick_lock:
            ready = [t for t in self.studio.journal.tasks.ready(lane) if t.stage in self.stages]
            if not ready:
                return None
            groups: dict[str, list[StageTask]] = {}
            for t in ready:
                groups.setdefault(t.residency, []).append(t)
            eligible = {r: g for r, g in groups.items() if self._eligible(r, g)}
            if not eligible:
                return None
            last, streak = self._last.get(lane, (None, 0))
            if last in eligible and (streak < self.limits.max_consecutive_passes or len(eligible) == 1):
                pick = last
            else:  # fairness boundary: another waiting group goes first
                others = {r: g for r, g in eligible.items() if r != last} or eligible
                pick = min(others, key=lambda r: (others[r][0].priority, others[r][0].seq))
            self._last[lane] = (pick, streak + 1 if pick == last else 1)
            return pick, eligible[pick][: self.limits.max_tasks_per_pass]

    # --- execution ------------------------------------------------------------------------------------------
    def _worker_of(self, task: StageTask) -> str | None:
        return getattr(self.stages[task.stage], "worker", None)

    def _loads(self, worker: str | None) -> dict[str, Any] | None:
        w = {"aux": self.studio.aux, "worker3d": self.studio.worker3d}.get(worker or "")
        if w is None:
            return None  # e.g. ComfyUI exposes no model-load counter: reported as unavailable, never as 0
        try:
            loads = w.health().get("loads")
        except Exception:
            return None
        return loads if isinstance(loads, dict) else None

    def run_pass(self, lane: str, residency: str, members: list[StageTask]) -> dict[str, Any]:
        tasks = self.studio.journal.tasks
        worker = self._worker_of(members[0])
        before = self._loads(worker)
        pid = tasks.open_pass(lane, residency, worker, before)
        prev = self._current.get(lane, {}).get("residency")
        self._current[lane] = {"pass_id": pid, "residency": residency, "task": None, "started": time.time()}
        started, done, jobs, reason = time.monotonic(), [], [], "exhausted"
        epoch = None
        if lane == "gpu1" and worker is not None:
            try:
                epoch = self.studio.lanes["gpu1"].acquire(worker)
            except Exception as e:  # ownership unknown: the resource blocks, its tasks stay queued
                _, err, _ = classify(e)
                self._blocked_until[residency] = time.monotonic() + RETRY_BLOCKED_S
                reason = f"resource_unavailable: {err['message'][:120]}"
                members = []
        queue, seen = list(members), {t.id for t in members}
        while queue:
            t = queue.pop(0)
            if not tasks.claim(t.id, pid):
                continue
            self._current[lane]["task"] = t.id
            task_started = time.monotonic()  # the block began no earlier than this (see _backing_off)
            _, resource_blocked = self._run_task(t)
            done.append(t.id)
            if t.job_id not in jobs and not is_id(t.job_id, "vdr"):  # variant drafts are not Jobs
                jobs.append(t.job_id)
            if resource_blocked:
                # Anchored at the task start, not at now: a retry queued right after the block is committed but
                # before this line runs must still count as "after the block" and bypass the backoff.
                self._blocked_until[residency] = task_started + RETRY_BLOCKED_S
                reason = "resource_unavailable"
                break
            if time.monotonic() - started > self.limits.max_pass_wall_s:
                reason = "fairness_wall_time"
                break
            if len(done) >= self.limits.max_tasks_per_pass:
                reason = "fairness_task_limit"
                break
            if not queue and not self._stop.is_set():
                # Compatible work that became ready during the pass (already authorized) joins it.
                more = [x for x in tasks.ready(lane) if x.residency == residency and x.id not in seen
                        and x.stage in self.stages]
                queue += more[: self.limits.max_tasks_per_pass - len(done)]
                seen |= {x.id for x in more}
        after = self._loads(worker)
        measured = {"model_loads": None if before is None or after is None else
                    {k: after.get(k, 0) - before.get(k, 0) for k in after},
                    "previous_residency": prev, "switched": prev not in (None, residency)}
        session = self.studio.lanes["gpu1"].sessions.get(worker or "") if lane == "gpu1" else None
        tasks.close_pass(pid, task_ids=done, jobs=jobs, reason=reason, loads_after=after, measured=measured,
                         session=session, epoch=epoch)
        self._current[lane] = {"residency": residency, "pass_id": None, "task": None}
        self.studio.events.publish("pass", lane=lane, pass_id=pid)
        return {"pass_id": pid, "tasks": done, "reason": reason}

    def _run_task(self, t: StageTask) -> tuple[str, bool]:
        tasks = self.studio.journal.tasks
        try:
            ctx = self.studio.registry.get(t.project_id)
        except Exception as e:  # project unavailable (moved/unmounted): keep the task, do not guess
            tasks.block(t.id, {"code": "project_unavailable", "message": str(e)[:300], "retryable": True})
            return "blocked", False
        env = TaskEnv(self.studio, ctx, t)
        stage = self.stages[t.stage]
        try:
            result = stage.run(env)
        except Exception as e:
            state, err, resource = classify(e)
            if state == "failed" and err["code"] == "internal_error":
                log.exception("task %s (%s) failed", t.id, t.stage)
            final = tasks.finish(t.id, state, error=err)
            if hook := getattr(stage, "on_error", None):
                try:
                    hook(env, final, err)
                except Exception:
                    log.exception("error hook for %s failed", t.id)
            self._publish(t)
            return final, resource
        from .reconcile import downstream_for

        follow: list[Any] | None
        try:
            follow = downstream_for(self.studio, ctx, t, result)
        except Exception:  # the result stays; None leaves downstream_ok=0 so startup reconciliation creates it
            log.exception("planning downstream of %s failed", t.id)
            follow = None
        final = tasks.complete(t.id, result, follow)
        self._publish(t)
        return final, False

    def _publish(self, t: StageTask) -> None:
        self.studio.events.publish("task", project_id=t.project_id, job_id=t.job_id, item_id=t.item_id,
                                   run_id=t.run_id, task_id=t.id)

    # --- retries --------------------------------------------------------------------------------------------
    def _retry_loop(self) -> None:
        while not self._stop.wait(RETRY_BLOCKED_S):
            self.retry_blocked()

    def retry_blocked(self) -> None:
        tasks = self.studio.journal.tasks
        for t in tasks.list(states=("blocked",)):
            err = t.error or {}
            if not err.get("retryable"):
                continue
            if t.attempts >= MAX_AUTO_ATTEMPTS or _age_s(t.created_at) > MAX_AUTO_RETRY_S:
                tasks.exhaust(t.id, err, t.attempts)
                continue
            tasks.retry(t.id)

    def status(self) -> dict[str, Any]:
        tasks = self.studio.journal.tasks
        return {"lanes": {lane: {"current": self._current.get(lane),
                                 "running": (self._current.get(lane) or {}).get("task"),
                                 "queued": len(tasks.list(lane=lane, states=("queued",))),
                                 "blocked": len(tasks.list(lane=lane, states=("blocked",)))} for lane in LANES},
                "limits": self.limits.__dict__, "passes": tasks.passes(limit=20)}


def wait_until(predicate: Callable[[], bool], timeout: float, interval: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
