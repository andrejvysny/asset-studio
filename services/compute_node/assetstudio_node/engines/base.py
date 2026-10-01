"""Engine-client helpers for runner adapters; re-exports the shared engine-call contracts."""
from __future__ import annotations

from typing import Any

from assetstudio_protocol.engine import (
    AckError,
    EngineImageHandle,
    EngineRejected,
    EngineState,
    EngineUnavailable,
    ExecutionCancelled,
    ExecutionFailed,
    ExecutionLost,
    ImageEditRequest,
    JobStatus,
    LoraUse,
    T2IRequest,
    engine_prompt_id,
)

__all__ = [
    "AckError",
    "EngineImageHandle",
    "EngineRejected",
    "EngineState",
    "EngineUnavailable",
    "ExecutionCancelled",
    "ExecutionFailed",
    "ExecutionLost",
    "ImageEditRequest",
    "JobStatus",
    "LoraUse",
    "T2IRequest",
    "engine_prompt_id",
    "post_ack",
]


def post_ack(http: Any, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Only an explicit 200 JSON object counts; anything else (timeout, reset, 409, malformed) is unknown."""
    try:
        r = http.post(path, json=payload, timeout=timeout)
    except Exception as e:  # httpx errors: the outcome is unknown, never "released"
        raise AckError(f"{path} not acknowledged: {type(e).__name__}") from e
    if r.status_code != 200:
        raise AckError(f"{path} not acknowledged: HTTP {r.status_code} {r.text[:120]}")
    try:
        body = r.json()
    except ValueError as e:
        raise AckError(f"{path} not acknowledged: malformed body") from e
    if not isinstance(body, dict):
        raise AckError(f"{path} not acknowledged: malformed body")
    return body
