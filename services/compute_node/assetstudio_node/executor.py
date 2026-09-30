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


class Executor(Protocol):
    def execute(self, offer: Offer, inputs: dict[str, Path], out_dir: Path,
                should_cancel: Callable[[], bool]) -> tuple[list[tuple[str, Path, str]], dict[str, Any]]:
        """Returns ([(name, path, mime)], meta). `inputs` maps each input sha256 to its local file. Must poll
        should_cancel and raise ExecutionCancelled."""
        ...

    def spooled(self, offer: Offer) -> None:
        """Called once the result manifest is durable, before the spooled report (R8): engines may free results."""
        ...


class FakeExecutor:
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
