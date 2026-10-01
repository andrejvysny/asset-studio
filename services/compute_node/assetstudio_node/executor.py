"""Executor interface between the agent and engine adapters, plus a deterministic simulated executor."""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from assetstudio_protocol.execution import Offer

FailureCode = Literal["input_invalid", "oom", "internal"]


class ExecutionCancelled(Exception):
    pass


class ExecutionFailed(Exception):
    def __init__(self, code: FailureCode, message: str = "", *, lost: bool = False) -> None:
        super().__init__(message or code)
        self.code = code
        self.lost = lost  # the worker restarted mid-execution: a retry is a new attempt, never a resubmission


class ExecutionBlocked(Exception):
    """A resource problem, not the item's fault: the engine is unreachable or GPU ownership is unknown."""

    def __init__(self, code: Literal["node_unavailable", "admission_rejected"], message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def engine_execution_id(offer: Offer) -> str:
    """Stable per attempt+generation and independent of the offer digest, so Studio's offers stay unchanged. Fits
    the 80-char X-Execution-Id limit of the aux service."""
    return f"{offer.attempt_id}-g{offer.generation}"


class Executor(Protocol):
    def reconciles(self, operation: str) -> bool:
        """True if an interrupted call of `operation` can be found again in the engine by its execution id."""
        ...

    def engine_execution_id(self, offer: Offer) -> str:
        """The id the engine knows this call by; persisted before the engine is called."""
        ...

    def execute(self, offer: Offer, inputs: dict[str, Path], out_dir: Path,
                should_cancel: Callable[[], bool]) -> tuple[list[tuple[str, Path, str]], dict[str, Any]]:
        """Returns ([(name, path, mime)], meta). `inputs` maps each input sha256 to its local file. Must poll
        should_cancel and raise ExecutionCancelled."""
        ...

    def spooled(self, offer: Offer) -> None:
        """Called once the result manifest is durable, before the spooled report (R8): engines may free results."""
        ...


def can_reconcile(executor: Any, operation: str) -> bool:
    found = getattr(executor, "reconciles", False)
    return bool(found(operation)) if callable(found) else bool(found)  # a bool attribute means "all operations"


def effective_engine_id(executor: Any, offer: Offer) -> str:
    fn = getattr(executor, "engine_execution_id", None)
    return fn(offer) if callable(fn) else engine_execution_id(offer)


class FakeExecutor:
    def reconciles(self, operation: str) -> bool:
        return False  # in-process: dies with the agent

    def engine_execution_id(self, offer: Offer) -> str:
        return engine_execution_id(offer)

    def __init__(self, *, fail_ops: dict[str, FailureCode] | None = None, delay_s: float = 0.0,
                 poll_s: float = 0.02) -> None:
        self.fail_ops = fail_ops or {}
        self.delay_s = delay_s
        self.poll_s = poll_s
        self.calls = 0

    def spooled(self, offer: Offer) -> None:
        pass

    def execute(self, offer: Offer, inputs: dict[str, Path], out_dir: Path,
                should_cancel: Callable[[], bool]) -> tuple[list[tuple[str, Path, str]], dict[str, Any]]:
        self.calls += 1
        if offer.operation in self.fail_ops:
            raise ExecutionFailed(self.fail_ops[offer.operation], f"simulated failure of {offer.operation}")
        deadline = time.monotonic() + self.delay_s
        while True:
            if should_cancel():
                raise ExecutionCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(self.poll_s, remaining))
        seed = json.dumps(offer.params, sort_keys=True).encode() + "".join(i.sha256 for i in offer.inputs).encode()
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "result.bin"
        path.write_bytes(b"SIMULATED:" + hashlib.sha256(seed).hexdigest().encode())
        return [("result.bin", path, "application/octet-stream")], {"simulated": True, "operation": offer.operation}
