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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.ids import derived_id, is_id

from ..adapters.base import AuxService, ImageEngine, Worker3dService
from ..journal import ADMISSION_PAUSED_KEY
from ..registry import ProjectContext
from ..studio import Studio
from ..taskstore import Busy, RunNotOpen, StageTask
from .errors import Blocked, Cancelled, ItemFailed, classify
from .node_pool import CLASS_OF_LANE, NodePool, worker_key

log = logging.getLogger("assetstudio.coordinator")
LANES = ("gpu0", "gpu1", "cpu")
RETRY_BLOCKED_S = 20.0
BACKOFF_SLACK_S = 0.01  # wall-clock ms truncation + wall/monotonic conversion jitter
# Automatic retries of transiently blocked work are bounded (no hidden infinite retry).
MAX_AUTO_ATTEMPTS = 6
MAX_AUTO_RETRY_S = 2 * 3600.0
MAX_ADMISSION_FAILURES = 6  # consecutive GPU1 acquire failures before queued members are blocked

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
    preferred_slot: tuple[str, str] | None = None  # node mode: (runner_id, slot_id) soft placement hint
    _calls: list[str] = field(default_factory=list)

    @property
    def engine(self) -> ImageEngine | None:
        return self.studio.execution.engine(self)

    @property
    def aux(self) -> AuxService | None:
        return self.studio.execution.aux(self)

    @property
    def worker3d(self) -> Worker3dService | None:
        return self.studio.execution.worker3d(self)

    @contextmanager
    def call(self, key: str) -> Iterator[None]:
        """Name the engine call in flight (docs/modular/compute-runner.md operation identity table)."""
        self._calls.append(key)
        try:
            yield
        finally:
            self._calls.pop()

    @property
    def current_call(self) -> str | None:
        return self._calls[-1] if self._calls else None

    def output_id(self, prefix: str, *parts: str, call: str | None = None) -> str:
        """Output artifact id: legacy form at generation 1, suffixed `g<N>` once a call was re-placed."""
        legacy = derived_id(prefix, *parts)
        generation = self.studio.execution.generation(self, call) if call is not None else 1
        return legacy if generation == 1 else derived_id(prefix, *parts, f"g{generation}")

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
        return self.studio.execution.acquire("gpu1", worker)


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
        self._admission_failures: dict[str, dict[str, Any]] = {}  # residency -> {"count", "since"}
        self._pause_read: tuple[float, bool] = (float("-inf"), False)
        self._pool = NodePool(self)
        self._task_worker: dict[str, str] = {}  # claimed task id -> worker key (kept out of _run_task's signature)
        self._preferred: dict[str, tuple[str, str]] = {}  # worker key -> slot of its last call in the pass

    # --- lifecycle ------------------------------------------------------------------------------------------
    def _kept_after_restart(self) -> frozenset[str]:
        """Node mode (R8): a running/reconciling task whose runner attempt is still open is never requeued, so
        its call is never re-placed on another runner; it reconciles against the attempt instead."""
        if self.studio.execution.mode != "nodes":
            return frozenset()
        from ..services._runner_util import NON_TERMINAL

        journal = self.studio.journal
        return frozenset(t.id for t in journal.tasks.list(states=("running", "reconciling"))
                         if journal.attempts.list(task_id=t.id, states=NON_TERMINAL, limit=1))

    def paused(self) -> bool:
        """`assetstudio execution switch` is draining the journal (R15); re-read at most once a second."""
        at, value = self._pause_read
        if time.monotonic() - at >= 1.0:
            value = self.studio.journal.meta_get(ADMISSION_PAUSED_KEY) == "1"
            self._pause_read = (time.monotonic(), value)
        return value

    def start(self) -> None:
        keep = self._kept_after_restart()
        rec = self.studio.journal.tasks.recover_after_restart(keep)
        orphans = rec.pop("orphans", [])
        if rec["requeued"] or rec["cancelled"] or keep:
            log.warning("restart reconciliation: %s (reconciling against live attempts: %d)", rec, len(keep))
        self.studio.execution.cancel_orphans(orphans)
        if keep:
            from ..services.runner_maintenance import reconcile_tasks

            reconcile_tasks(self.studio)
        from .reconcile import reconcile_on_start

        reconcile_on_start(self.studio)
        nodes = self.studio.execution.mode == "nodes"
        workers = ([] if nodes else [("gpu0", 0), ("gpu1", 0)]) + [("cpu", i) for i in range(self.limits.cpu_workers)]
        for lane, i in workers:
            self._start_thread(self._loop, (lane,), f"lane-{lane}-{i}")
        self._start_thread(self._retry_loop, (), "retry-blocked")
        if nodes:
            self._pool.refresh()
            self._start_thread(self._pool_loop, (), "node-pool")

    def _start_thread(self, target: Callable[..., None], args: tuple[Any, ...], name: str) -> None:
        t = threading.Thread(target=target, args=args, name=name, daemon=True)
        t.start()
        with self._pick_lock:
            self._threads = [x for x in self._threads if x.is_alive()] + [t]

    def spawn_worker(self, lane: str, k: int) -> None:
        self._start_thread(self._loop, (lane, k), f"lane-{lane}-{k}")

    def _pool_loop(self) -> None:
        while not self._stop.wait(self.studio.settings.runner_maintenance_s):
            try:
                self._pool.refresh()
            except Exception:  # the pools keep their last target
                log.exception("node pool refresh failed")

    def stop(self) -> None:
        self._stop.set()
        with self.studio.journal.changed:
            self.studio.journal.changed.notify_all()
        for t in self._threads:
            t.join(timeout=5)

    def _loop(self, lane: str, k: int | None = None) -> None:
        key = worker_key(lane, k)
        try:
            while not self._stop.is_set():
                try:
                    picked = self.choose(lane, key)
                    if picked is None:
                        if k is not None and self._pool.should_exit(lane, k):
                            return
                        with self.studio.journal.changed:
                            self.studio.journal.changed.wait(timeout=1.0)
                        continue
                    self.run_pass(lane, *picked, key=key)
                except Exception:  # supervision: a scheduler bug is logged; the lane keeps running
                    log.exception("lane %s loop error", key)
                    time.sleep(1.0)
        finally:
            if k is not None:
                self._pool.left(lane, k)

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

    def choose(self, lane: str, key: str | None = None) -> tuple[str, list[StageTask]] | None:
        """Pick the residency group to run next on `lane`: stay on the resident model while it has work (bounded
        by max_consecutive_passes), otherwise the group holding the highest-priority / oldest ready task."""
        if self.paused():
            return None
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
            key = key or lane  # fairness streaks are per worker
            last, streak = self._last.get(key, (None, 0))
            if last in eligible and (streak < self.limits.max_consecutive_passes or len(eligible) == 1):
                pick = last
            else:  # fairness boundary: another waiting group goes first
                others = {r: g for r, g in eligible.items() if r != last} or eligible
                pick = min(others, key=lambda r: (others[r][0].priority, others[r][0].seq))
            self._last[key] = (pick, streak + 1 if pick == last else 1)
            return pick, eligible[pick][: self.limits.max_tasks_per_pass]

    # --- execution ------------------------------------------------------------------------------------------
    def _worker_of(self, task: StageTask) -> str | None:
        return getattr(self.stages[task.stage], "worker", None)

    def _loads(self, worker: str | None) -> dict[str, Any] | None:
        w = {"aux": self.studio.execution.aux(), "worker3d": self.studio.execution.worker3d()}.get(worker or "")
        if w is None:
            return None  # e.g. ComfyUI exposes no model-load counter: reported as unavailable, never as 0
        try:
            loads = w.health().get("loads")
        except Exception:
            return None
        return loads if isinstance(loads, dict) else None

    def run_pass(self, lane: str, residency: str, members: list[StageTask],
                 key: str | None = None) -> dict[str, Any]:
        tasks = self.studio.journal.tasks
        key = key or lane  # node workers record passes under `<lane>#<k>`
        self._preferred.pop(key, None)
        worker = self._worker_of(members[0])
        before = self._loads(worker)
        pid = tasks.open_pass(key, residency, worker, before)
        prev = self._current.get(key, {}).get("residency")
        self._current[key] = {"pass_id": pid, "residency": residency, "task": None, "started": time.time()}
        started, done, jobs, reason = time.monotonic(), [], [], "exhausted"
        epoch = None
        if lane == "gpu1" and worker is not None:
            try:
                epoch = self.studio.execution.acquire("gpu1", worker)
                self._admission_failures.pop(residency, None)
                for t in members:
                    if t.progress.pop("admission", None) is not None:
                        tasks.progress(t.id, t.progress, touch=False)
            except Exception as e:  # ownership unknown: the resource blocks, its tasks stay queued (bounded)
                _, err, _ = classify(e)
                self._blocked_until[residency] = time.monotonic() + RETRY_BLOCKED_S
                reason = f"resource_unavailable: {err['message'][:120]}"
                self._admission_failed(residency, worker, members, err["message"])
                members = []
        queue, seen = list(members), {t.id for t in members}
        while queue:
            if self.paused():
                reason = "admission_paused"
                break
            t = queue.pop(0)
            if not tasks.claim(t.id, pid):
                continue
            self._current[key]["task"] = t.id
            self._task_worker[t.id] = key
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
        gpu1 = self.studio.lanes.get("gpu1")
        session = gpu1.sessions.get(worker or "") if lane == "gpu1" and gpu1 is not None else None
        tasks.close_pass(pid, task_ids=done, jobs=jobs, reason=reason, loads_after=after, measured=measured,
                         session=session, epoch=epoch)
        self._current[key] = {"residency": residency, "pass_id": None, "task": None}
        self.studio.events.publish("pass", lane=key, pass_id=pid)
        return {"pass_id": pid, "tasks": done, "reason": reason}

    def _admission_failed(self, residency: str, worker: str, members: list[StageTask], message: str) -> None:
        tasks = self.studio.journal.tasks
        rec = self._admission_failures.setdefault(residency, {"count": 0, "since": now_iso()})
        rec["count"] += 1
        for t in members:
            t.progress["admission"] = {"code": "resource_unavailable", "message": message[:200],
                                       "failures": rec["count"], "since": rec["since"]}
            tasks.progress(t.id, t.progress, touch=False)
        if rec["count"] >= MAX_ADMISSION_FAILURES:
            n = rec["count"]
            tasks.block_queued([t.id for t in members], {
                "code": "resource_unavailable", "retryable": False,
                "message": f"could not acquire GPU1 for {worker} after {n} attempts: {message[:200]}; "
                           "retry explicitly"})
            self._admission_failures.pop(residency, None)
            for t in members:
                self._publish(t)

    def _run_task(self, t: StageTask) -> tuple[str, bool]:
        try:
            return self._execute(t)
        finally:
            from .reconcile import retry_deferred

            self._task_worker.pop(t.id, None)
            retry_deferred(self.studio, t.project_id, t.item_id)

    def _execute(self, t: StageTask) -> tuple[str, bool]:
        tasks = self.studio.journal.tasks
        try:
            ctx = self.studio.registry.get(t.project_id)
        except Exception as e:  # project unavailable (moved/unmounted): keep the task, do not guess
            tasks.block(t.id, {"code": "project_unavailable", "message": str(e)[:300], "retryable": True})
            return "blocked", False
        key = self._task_worker.get(t.id, t.lane)
        env = TaskEnv(self.studio, ctx, t, preferred_slot=self._preferred.get(key))
        stage = self.stages[t.stage]
        try:
            try:
                result = stage.run(env)
            finally:
                if env.preferred_slot is not None:
                    self._preferred[key] = env.preferred_slot
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
            self._task_finished(t.id, final)
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
        self._task_finished(t.id, final)
        self._publish(t)
        return final, False

    def _task_finished(self, task_id: str, state: str) -> None:
        try:
            self.studio.execution.task_finished(task_id, state)
        except Exception:  # custody bookkeeping must never change a task's outcome
            log.exception("settling execution results of task %s failed", task_id)

    def _publish(self, t: StageTask) -> None:
        self.studio.events.publish("task", project_id=t.project_id, job_id=t.job_id, item_id=t.item_id,
                                   run_id=t.run_id, task_id=t.id)

    # --- retries --------------------------------------------------------------------------------------------
    def _retry_loop(self) -> None:
        while not self._stop.wait(RETRY_BLOCKED_S):
            self.retry_blocked()
            from .reconcile import retry_deferred

            retry_deferred(self.studio)

    def retry_blocked(self) -> None:
        tasks = self.studio.journal.tasks
        for t in tasks.list(states=("blocked",)):
            err = t.error or {}
            if not err.get("retryable"):
                continue
            if t.attempts >= MAX_AUTO_ATTEMPTS or _age_s(t.created_at) > MAX_AUTO_RETRY_S:
                tasks.exhaust(t.id, err, t.attempts)
                continue
            try:
                tasks.retry(t.id)
            except (Busy, RunNotOpen) as e:  # another owner / closed run: stays blocked for an operator decision
                log.warning("automatic retry of %s refused: %s", t.id, e)

    def _lane_status(self, lane: str) -> dict[str, Any]:
        tasks = self.studio.journal.tasks
        keys = self._pool.keys(lane) if lane in CLASS_OF_LANE and self.studio.execution.mode == "nodes" else [lane]
        currents = {k: self._current.get(k) for k in keys}
        busy = next((c for c in currents.values() if c and c.get("task")), None)
        out: dict[str, Any] = {"current": busy or currents[keys[0]] if keys else None,
                               "running": (busy or {}).get("task"),
                               "queued": len(tasks.list(lane=lane, states=("queued",))),
                               "blocked": len(tasks.list(lane=lane, states=("blocked",)))}
        if keys != [lane]:
            out["workers"] = currents
            out["pool_target"] = self._pool.target(lane)
        return out

    def status(self) -> dict[str, Any]:
        tasks = self.studio.journal.tasks
        return {"lanes": {lane: self._lane_status(lane) for lane in LANES},
                "limits": self.limits.__dict__, "passes": tasks.passes(limit=20),
                "admission_failures": {r: dict(v) for r, v in self._admission_failures.items()}}


def wait_until(predicate: Callable[[], bool], timeout: float, interval: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
