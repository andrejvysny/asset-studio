"""One blocking engine call executed as a runner attempt (R5, R6, R8, R12).

Stage code is synchronous and keeps its engine-call shape; this helper offers the call to the runner fleet, waits for
the attempt to reach a result and maps every attempt outcome onto the exceptions the stages already handle.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from assetstudio_protocol.base import Msg
from assetstudio_protocol.execution import (
    OPERATION_CAPABILITY,
    OPERATION_ENGINE,
    InputRef,
    Requirements,
)

from ..adapters.base import (
    EngineUnavailable,
    ExecutionCancelled,
    ExecutionFailed,
    ExecutionLost,
)
from ..coordinator.errors import Cancelled
from ..runner_errors import RunnerError
from ..services import attempts, transfers

if TYPE_CHECKING:
    from ..coordinator.runner import TaskEnv
    from ..studio import Studio

__all__ = ["DEFAULT_WAIT_S", "image_mime", "read_result", "result_simulated", "run_call", "run_stage_call"]

DEFAULT_WAIT_S = 30 * 60.0
POLL_S = 0.2
_PLACE_EVERY_S = 1.0
_MODEL = re.compile(r"[A-Za-z0-9_]+@[0-9a-f]{12}")
_RESULT = ("ingested", "committed")
_INPUT_CODES = ("invalid_input", "validation_failed")
_RESOURCE_CODES = ("node_unavailable", "admission_rejected")
_RUNNING = ("offered", "leased", "admitted", "executing", "spooled", "uploading")
Input = tuple[bytes, str, str, str]  # (bytes, role, label, mime)


def image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    return "image/jpeg" if data.startswith(b"\xff\xd8") else "application/octet-stream"


def _requirements(env: TaskEnv, operation: str) -> Requirements:
    residency = env.task.residency
    return Requirements(capability=OPERATION_CAPABILITY[operation], engine=OPERATION_ENGINE[operation],
                        models=sorted(set(_MODEL.findall(residency))), resource_profile=residency)


def _failure(a: dict[str, Any]) -> Exception:
    err = a["error"] or {}
    code, msg = err.get("code"), str(err.get("message") or "attempt failed")
    if code in _INPUT_CODES:
        return ExecutionFailed(msg, "input_invalid")
    if code == "resource_exhausted":
        return ExecutionFailed(msg, "oom")
    if code in _RESOURCE_CODES:
        return EngineUnavailable(msg)
    if code == "uncertain_execution":
        return ExecutionLost(msg)
    return ExecutionFailed(msg, "internal")


def read_result(env: TaskEnv, a: dict[str, Any]) -> tuple[dict[str, bytes], dict[str, Any]]:
    manifest = a["manifest"] or {}
    repo = env.ctx.store.repo
    files = {f["name"]: repo.read_blob_verified(f["sha256"]) for f in manifest.get("files", [])}
    return files, dict(manifest.get("meta") or {})


def result_simulated(studio: Studio, task_id: str) -> bool:
    """True iff any finished call of the task ran on a simulated engine (stateless: adapters are per access)."""
    return any(((a["manifest"] or {}).get("meta") or {}).get("simulated") is True
               for a in studio.journal.attempts.list(task_id=task_id) if a["manifest"])


def _note(env: TaskEnv, call_key: str, a: dict[str, Any]) -> None:
    remote = dict(env.task.progress.get("remote") or {})
    entry = {"attempt_id": a["id"], "generation": a["generation"], "state": a["state"],
             "runner_id": a["runner_id"], "slot_id": a["slot_id"]}
    if remote.get(call_key) != entry:
        env.progress(remote={**remote, call_key: entry})


def _settled(env: TaskEnv, a: dict[str, Any]) -> tuple[dict[str, bytes], dict[str, Any]] | None:
    state = a["state"]
    if state in _RESULT:
        return read_result(env, a)
    if state == "failed":
        raise _failure(a)
    if state == "cancelled":
        raise ExecutionCancelled(f"attempt {a['id']} was cancelled")
    if state == "lost":
        raise ExecutionLost(f"attempt {a['id']} was declared lost")
    if state == "quarantined":
        raise EngineUnavailable("superseded attempt")
    if state == "uncertain":
        raise EngineUnavailable("runner lost contact; attempt uncertain until it reconnects or an operator "
                                "declares it lost")
    return None


def _cancel_requested(env: TaskEnv, attempt_id: str, should_cancel: Callable[[], bool] | None) -> None:
    try:
        env.check_cancel()
    except Cancelled:
        attempts.cancel(env.studio, attempt_id)
        raise
    if should_cancel is not None and should_cancel():
        attempts.cancel(env.studio, attempt_id)
        raise ExecutionCancelled("cancelled by the caller")


def _unplaced_problem(studio: Studio, a: dict[str, Any], waited_s: float) -> str | None:
    if a["state"] != "offered" or a["runner_id"] is not None or waited_s <= studio.settings.runner_offer_ttl_s:
        return None
    reasons = list((a["progress"].get("placement") or {}).get("reasons") or ["no runner is registered"])
    if any("already has an attempt" in r for r in reasons):
        return None  # a busy slot frees by itself: keep waiting within the wait budget
    return "no eligible runner: " + "; ".join(reasons)


def _wait(env: TaskEnv, attempt_id: str, call_key: str, should_cancel: Callable[[], bool] | None,
          wait_s: float) -> tuple[dict[str, bytes], dict[str, Any]]:
    studio, started, last_place = env.studio, time.monotonic(), 0.0
    a: dict[str, Any] = {}
    try:
        while True:
            a = studio.journal.attempts.get(attempt_id) or a
            done = _settled(env, a)
            if done is not None:
                return done
            _cancel_requested(env, attempt_id, should_cancel)
            waited = time.monotonic() - started
            if (problem := _unplaced_problem(studio, a, waited)) is not None:
                raise EngineUnavailable(problem)
            if waited > wait_s:
                raise EngineUnavailable("attempt exceeded its wait budget")
            if a["state"] == "offered" and time.monotonic() - last_place >= _PLACE_EVERY_S:
                last_place = time.monotonic()
                attempts.place(studio, attempt_id)
            with studio.journal.changed:
                studio.journal.changed.wait(timeout=POLL_S)
    finally:
        if a:
            _note(env, call_key, studio.journal.attempts.get(attempt_id) or a)


def run_call(studio: Studio, env: TaskEnv, operation: str, params: Msg, inputs: list[Input], *,
             should_cancel: Callable[[], bool] | None = None,
             wait_s: float = DEFAULT_WAIT_S) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Blocks until the call's attempt holds a result. Returns (files by name, result meta)."""
    call_key = env.current_call or operation
    refs = [InputRef(sha256=transfers.stage_input(studio, data), size=len(data), role=role, label=label, mime=mime)
            for data, role, label, mime in inputs]
    try:
        row = attempts.offer_call(studio, task_id=env.task.id, call_key=call_key, project_id=env.ctx.id,
                                  operation=operation, inputs=refs, params=params.model_dump(mode="json"),
                                  requirements=_requirements(env, operation))
    except RunnerError as e:  # stale_revision: the call key was offered with different inputs
        raise ExecutionFailed(e.message, "input_invalid") from e
    _note(env, call_key, row)
    return _wait(env, row["id"], call_key, should_cancel, wait_s)


def run_stage_call(studio: Studio, env: TaskEnv, operation: str, params: Msg, inputs: list[Input], *,
                   wait_s: float = DEFAULT_WAIT_S) -> tuple[dict[str, bytes], dict[str, Any]]:
    """run_call for stages that only know the direct engines: cancellation is the task's Cancelled, and a lost
    attempt is a resource problem (the retry re-enters and opens the next generation)."""
    try:
        return run_call(studio, env, operation, params, inputs, wait_s=wait_s)
    except ExecutionCancelled as e:
        raise Cancelled() from e
    except ExecutionLost as e:
        raise EngineUnavailable(f"{e}; a retry opens a new attempt") from e
