"""Stage-facing engine protocols. Engine contracts live in assetstudio_protocol.engine, adapters in
assetstudio_node.engines (re-exported here until direct mode is removed, WP2.10)."""
from __future__ import annotations

from typing import Any, Protocol

from assetstudio_node.engines.base import post_ack
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
    "AuxService",
    "EngineImageHandle",
    "EngineRejected",
    "EngineState",
    "EngineUnavailable",
    "ExecutionCancelled",
    "ExecutionFailed",
    "ExecutionLost",
    "ImageEditRequest",
    "ImageEngine",
    "JobStatus",
    "LeasedWorker",
    "LoraUse",
    "T2IRequest",
    "Worker3dService",
    "engine_prompt_id",
    "post_ack",
]


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
