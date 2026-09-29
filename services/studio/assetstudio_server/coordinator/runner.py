"""Single scheduler: one worker thread per resource lane, affinity-grouped passes, restart reconciliation."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..adapters.base import EngineUnavailable
from ..gpu import OwnershipUnknown
from ..journal import Operation
from ..registry import ProjectContext
from ..studio import Studio

log = logging.getLogger("assetstudio.coordinator")
LANES = ("gpu0", "gpu1", "cpu")
MAX_AFFINITY_STREAK = 6  # bound model-affinity preference so other ready batches are not starved
RETRY_BLOCKED_S = 20.0
# Automatic retries of transiently blocked work are bounded (no hidden infinite retry): after this many attempts or
# this much elapsed time the operation stays blocked for an explicit operator decision.
MAX_AUTO_ATTEMPTS = 6
MAX_AUTO_RETRY_S = 2 * 3600.0


class Cancelled(Exception):
    pass


class Blocked(Exception):
    """Cannot proceed right now (engine unreachable, ownership unknown). Retryable unless `operator` is set."""

    def __init__(self, message: str, code: str = "blocked", operator: bool = False) -> None:
        super().__init__(message)
        self.code, self.operator = code, operator


@dataclass
class TaskEnv:
    studio: Studio
    ctx: ProjectContext
    op: Operation

    def progress(self, **fields: Any) -> None:
        self.op.progress.update(fields)
        self.studio.journal.update(self.op.id, progress=self.op.progress)
        self.studio.events.publish("operation", project_id=self.ctx.id, batch_id=self.op.batch_id,
                                   op_id=self.op.id, progress=self.op.progress)

    def engine_state(self, **fields: Any) -> None:
        self.op.engine.update(fields)
        self.studio.journal.update(self.op.id, engine=self.op.engine)

    def check_cancel(self) -> None:
        cur = self.studio.journal.get(self.op.id)
        if cur is not None and cur.state == "cancel_requested":
            raise Cancelled()

    def check_cancel_or(self, on_cancel: Callable[[], Any]) -> None:
        """Cancellation hook for engine work this operation exclusively owns (e.g. its own prompt id)."""
        try:
            self.check_cancel()
        except Cancelled:
            on_cancel()
            raise


Handler = Callable[[TaskEnv], dict[str, Any]]
ErrorHook = Callable[[TaskEnv, str, str], None]


class Coordinator:
    def __init__(self, studio: Studio, handlers: dict[str, tuple[Handler, ErrorHook]]) -> None:
        self.studio = studio
        self.handlers = handlers
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last: dict[str, tuple[str | None, int]] = {lane: (None, 0) for lane in LANES}
        self.running: dict[str, str | None] = {lane: None for lane in LANES}

    def start(self) -> None:
        self.studio.journal.release_all_held()  # a crash between enqueue and release must not strand work
        for op in self.studio.journal.mark_running_as_reconciling():
            # Handlers are idempotent and reconcile engine-side work by deterministic ids before resubmitting.
            op.progress["reconciled_after_restart"] = True
            self.studio.journal.update(op.id, progress=op.progress)
            self.studio.journal.requeue(op.id)
            log.warning("reconciling %s %s after restart", op.kind, op.id)
        for lane in LANES:
            t = threading.Thread(target=self._loop, args=(lane,), name=f"lane-{lane}", daemon=True)
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

    def _claim(self, lane: str) -> Operation | None:
        aff, streak = self._last[lane]
        op = self.studio.journal.claim_next(lane, self.studio.settings.instance_id,
                                            aff if streak < MAX_AFFINITY_STREAK else None)
        if op is not None:
            self._last[lane] = (op.affinity, streak + 1 if op.affinity == aff else 1)
        return op

    def _loop(self, lane: str) -> None:
        while not self._stop.is_set():
            op = self._claim(lane)
            if op is None:
                with self.studio.journal.changed:
                    self.studio.journal.changed.wait(timeout=2.0)
                continue
            self.running[lane] = op.id
            try:
                self.run_one(op)
            finally:
                self.running[lane] = None

    def run_one(self, op: Operation) -> None:
        handler, on_error = self.handlers[op.kind]
        try:
            ctx = self.studio.registry.get(op.project_id)
        except Exception as e:  # project unavailable (moved/unmounted): keep the op, do not guess
            self.studio.journal.finish(op.id, "blocked", error={"code": "project_unavailable", "message": str(e),
                                                                "retryable": True})
            return
        env = TaskEnv(self.studio, ctx, op)
        try:
            result = handler(env)
        except Cancelled:
            self.studio.journal.finish(op.id, "cancelled", error={"code": "cancelled", "message": "cancelled"})
            on_error(env, "cancelled", "cancelled by operator")
        except OwnershipUnknown as e:
            self.studio.journal.finish(op.id, "blocked", error={"code": e.code, "message": str(e),
                                                                "retryable": False})
            on_error(env, "blocked", str(e))
        except (Blocked, EngineUnavailable) as e:
            operator = isinstance(e, Blocked) and e.operator
            self.studio.journal.finish(op.id, "blocked", error={
                "code": getattr(e, "code", "engine_unavailable"), "message": str(e), "retryable": not operator})
            on_error(env, "blocked", str(e))
        except Exception as e:  # unexpected: record, never swallow silently
            log.exception("operation %s failed", op.id)
            self.studio.journal.finish(op.id, "failed", error={"code": "internal_error",
                                                               "message": f"{type(e).__name__}: {e}"[:500]})
            on_error(env, "failed", f"{type(e).__name__}: {e}"[:300])
        else:
            self.studio.journal.finish(op.id, "succeeded", result=result)
        self.studio.events.publish("operation", project_id=op.project_id, batch_id=op.batch_id, op_id=op.id)

    def _retry_loop(self) -> None:
        while not self._stop.wait(RETRY_BLOCKED_S):
            self.retry_blocked()

    def retry_blocked(self) -> None:
        for op in self.studio.journal.list(states=("blocked",)):
            err = op.error or {}
            if not err.get("retryable"):
                continue
            if op.attempts >= MAX_AUTO_ATTEMPTS or _age_s(op.created_at) > MAX_AUTO_RETRY_S:
                self.studio.journal.finish(op.id, "blocked", error={
                    **err, "retryable": False, "retry_budget_exhausted": True,
                    "message": f"{err.get('message', '')} (automatic retries exhausted after {op.attempts} "
                               "attempts; retry explicitly)"[:500]})
                continue
            self.studio.journal.requeue(op.id, ("blocked",))

    def status(self) -> dict[str, Any]:
        return {"lanes": {lane: {"running": self.running[lane],
                                 "queued": len(self.studio.journal.list(lane=lane, states=("queued",)))}
                          for lane in LANES}}


def _age_s(iso: str) -> float:
    from datetime import UTC, datetime

    try:
        return (datetime.now(UTC) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds()
    except ValueError:
        return 0.0


def wait_until(predicate: Callable[[], bool], timeout: float, interval: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def release_cancelled(studio: Studio, op: Operation) -> None:
    """An operation cancelled before it ran: release its items' task markers so the rows are actionable again."""
    from .handlers import HANDLERS

    env = TaskEnv(studio, studio.registry.get(op.project_id), op)
    HANDLERS[op.kind][1](env, "cancelled", "cancelled before start")
