"""Engine adapter contracts. Workers receive typed inputs and return bytes/metadata; they own no product state."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

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


class ImageEngine(Protocol):
    name: str
    simulated: bool

    def check(self) -> dict[str, Any]: ...

    def submit(self, req: T2IRequest) -> str: ...

    def status(self, prompt_id: str) -> JobStatus: ...

    def submit_edit(self, req: ImageEditRequest) -> dict[str, Any]:
        """Receipt: {prompt_id, workflow, workflow_version, graph_sha256, input: {name, subfolder, sha256}}."""
        ...

    def supports(self, kind: str) -> bool: ...

    def fetch_image(self, prompt_id: str, workflow_id: str | None = None) -> bytes: ...

    def cancel(self, prompt_id: str) -> dict[str, Any]: ...

    def describe(self) -> dict[str, Any]: ...


class LeasedWorker(Protocol):
    name: str
    simulated: bool

    def health(self) -> dict[str, Any]: ...

    def lease(self, epoch: int) -> dict[str, Any]: ...

    def unload(self, owner_token: str, epoch: int) -> dict[str, Any]: ...


class AuxService(LeasedWorker, Protocol):
    def enhance(self, *, brief: str, kind: str, constraints: str, style_guide: str, epoch: int,
                execution_id: str | None = None, preset: str = "conservative", mode: str = "t2i",
                images: list[tuple[bytes, str, str]] | None = None, preserve: str = "",
                change: str = "") -> dict[str, Any]: ...

    def compare(self, *, images: list[tuple[bytes, str, str]], questions: list[tuple[str, str]], context: str,
                epoch: int, execution_id: str | None = None) -> dict[str, Any]: ...

    def analyze_source(self, *, images: list[tuple[bytes, str]], kind: str, user_facts: str = "", epoch: int,
                       execution_id: str | None = None) -> dict[str, Any]: ...

    def suggest_variants(self, *, images: list[tuple[bytes, str]], request: str, count: int, intent: str,
                         preserve: str, kind: str, observations: list[str] | None = None, epoch: int,
                         execution_id: str | None = None) -> dict[str, Any]: ...

    def qa(self, *, image: bytes, questions: list[tuple[str, str]], context: str, epoch: int,
           execution_id: str | None = None) -> dict[str, Any]: ...

    def cutout(self, *, image: bytes, epoch: int, execution_id: str | None = None) -> dict[str, Any]: ...


class Worker3dService(LeasedWorker, Protocol):
    def status(self, execution_id: str) -> dict[str, Any] | None: ...

    def execute(self, execution_id: str, op: str, params: dict[str, Any], body: bytes, *, epoch: int,
                should_cancel: Any = None) -> tuple[bytes, dict[str, Any]]: ...

    def ack(self, execution_id: str) -> None: ...
