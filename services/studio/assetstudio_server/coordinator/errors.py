"""Failure taxonomy for stage tasks: what failed (one item vs. a shared resource) decides what continues."""
from __future__ import annotations

from typing import Any

from assetstudio_storage.repo import CorruptBlob

from ..adapters.base import EngineRejected, EngineUnavailable, ExecutionFailed
from ..gpu import OwnershipUnknown


class Cancelled(Exception):
    pass


class Blocked(Exception):
    """Cannot proceed right now (engine unreachable, ownership unknown). Retryable unless `operator` is set.
    Scope is the RESOURCE: the pass stops, other lanes and other resources continue."""

    def __init__(self, message: str, code: str = "blocked", operator: bool = False) -> None:
        super().__init__(message)
        self.code, self.operator = code, operator


class ItemFailed(Exception):
    """This item's inputs/outputs are the problem (invalid input, invalid output, OOM for this profile).
    Scope is the ITEM: unaffected items and Jobs continue."""

    def __init__(self, message: str, code: str = "input_invalid") -> None:
        super().__init__(message)
        self.code = code


def classify(e: BaseException) -> tuple[str, dict[str, Any], bool]:
    """-> (task state, error body, resource_blocked). Unknown exceptions are programmer defects: recorded with
    their type, the task fails, the lane keeps serving other work."""
    if isinstance(e, Cancelled):
        return "cancelled", {"code": "cancelled", "message": "cancelled by operator"}, False
    if isinstance(e, OwnershipUnknown):
        return "blocked", {"code": e.code, "message": str(e), "retryable": False,
                           "action": "reset the GPU lane after confirming the workers are idle"}, True
    if isinstance(e, Blocked):
        return "blocked", {"code": e.code, "message": str(e), "retryable": not e.operator}, True
    if isinstance(e, EngineUnavailable):
        return "blocked", {"code": "engine_unavailable", "message": str(e)[:500], "retryable": True}, True
    if isinstance(e, CorruptBlob):
        return "blocked", {"code": "artifact_corrupt", "message": str(e), "retryable": False,
                           "action": "run `assetstudio storage verify` and repair from a verified source"}, False
    if isinstance(e, ItemFailed):
        return "failed", {"code": e.code, "message": str(e)[:500], "retryable": e.code in ("oom", "internal")}, False
    if isinstance(e, ExecutionFailed):
        return "failed", {"code": e.code, "message": str(e)[:500], "retryable": e.code != "input_invalid"}, False
    if isinstance(e, EngineRejected):
        return "failed", {"code": "input_invalid", "message": str(e)[:500], "retryable": False}, False
    return "failed", {"code": "internal_error", "message": f"{type(e).__name__}: {e}"[:500], "retryable": True}, False
