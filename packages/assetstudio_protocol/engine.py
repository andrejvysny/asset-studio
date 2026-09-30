"""Engine-call contracts shared by Studio and runners: request/status types and engine error taxonomy."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

EngineState = Literal["pending", "running", "succeeded", "failed", "unknown"]
_NS = uuid.UUID("5d1c9a52-8f3e-4b6a-9c2d-7a41f0e6b3a9")


class EngineUnavailable(Exception):
    """Could not reach the engine. For a submit this is UNCERTAIN: reconcile by prompt id before resubmitting."""


class EngineRejected(Exception):
    """The engine refused the request (validation). Retrying the same input will not help."""


class AckError(Exception):
    """A worker did not positively acknowledge an ownership/unload request."""


class ExecutionFailed(EngineRejected):
    """A worker execution reached a terminal failure. `code`: input_invalid | oom | internal | cancelled."""

    def __init__(self, message: str, code: str = "internal") -> None:
        super().__init__(message)
        self.code = code


class ExecutionLost(Exception):
    """The worker restarted before the execution finished and nothing was spooled: the computation is gone.
    A retry is an explicit new attempt (new execution id), never an implicit resubmission."""


class ExecutionCancelled(Exception):
    """The execution was cancelled (by request) before producing a result."""


@dataclass(frozen=True)
class LoraUse:
    file: str
    strength: float


@dataclass(frozen=True)
class T2IRequest:
    prompt_id: str
    positive: str
    negative: str
    seed: int
    width: int
    height: int
    steps: int
    cfg: float
    filename_prefix: str
    style_lora: LoraUse | None = None
    speed_lora: LoraUse | None = None


@dataclass(frozen=True)
class EngineImageHandle:
    """A source image known to be in the engine's input store. Built only by the adapter after a verified upload."""
    name: str
    subfolder: str
    sha256: str


@dataclass(frozen=True)
class ImageEditRequest:
    prompt_id: str
    source_sha256: str
    prepared_input_sha256: str
    image: bytes  # verified PNG bytes of the prepared single-object input, never a path/URL
    positive: str
    negative: str
    seed: int
    steps: int
    cfg: float
    filename_prefix: str
    output_profile_id: str = "edit.default"
    recipe_version: int = 1


@dataclass
class JobStatus:
    state: EngineState
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def engine_prompt_id(*parts: str) -> str:
    """Deterministic UUID: a lost submit response can be reconciled by looking the id up in the engine."""
    return str(uuid.uuid5(_NS, "/".join(parts)))
