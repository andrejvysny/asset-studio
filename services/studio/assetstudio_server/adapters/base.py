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

    def fetch_image(self, prompt_id: str) -> bytes: ...

    def cancel(self, prompt_id: str) -> dict[str, Any]: ...

    def describe(self) -> dict[str, Any]: ...


class AuxService(Protocol):
    name: str
    simulated: bool

    def health(self) -> dict[str, Any]: ...

    def enhance(self, *, brief: str, kind: str, constraints: str, style_guide: str) -> dict[str, Any]: ...

    def qa(self, *, image: bytes, questions: list[tuple[str, str]], context: str) -> dict[str, Any]: ...

    def cutout(self, *, image: bytes) -> dict[str, Any]: ...

    def unload(self, owner_token: str) -> dict[str, Any]: ...
