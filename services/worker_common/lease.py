"""GPU-worker lease: fencing epoch + worker session + activity accounting over ALL GPU work (Python 3.10+).

Copied verbatim into the aux and worker3d images. Protocol (Studio side: gpu.GpuLane):
- POST /lease {epoch}: the Studio grants this worker the device. A lower epoch than already seen is stale (409):
  a Studio process that lost ownership can never submit again.
- Every GPU request carries `x-lease-epoch`; it must equal the granted epoch while the worker is admitting.
- POST /unload {epoch}: stop admitting, wait until no request is queued or running (generation, export, tensor
  transfer, preprocessing all count), release weights, and only then acknowledge {active: 0, loaded: false}.
"""
from __future__ import annotations

import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class StaleLease(Exception):
    """Wrong/old epoch or the worker is not admitting (drained, never leased, restarted)."""


class Lease:
    def __init__(self) -> None:
        self.session_id = secrets.token_hex(8)  # changes on every worker restart
        self.epoch = 0
        self.admitting = False
        self.active = 0  # running + queued GPU work
        self._cond = threading.Condition()

    def info(self) -> dict[str, Any]:
        with self._cond:
            return {"session_id": self.session_id, "epoch": self.epoch, "admitting": self.admitting,
                    "active": self.active}

    def grant(self, epoch: int) -> dict[str, Any]:
        with self._cond:
            if epoch < self.epoch:
                raise StaleLease(f"epoch {epoch} is older than {self.epoch}")
            if epoch > self.epoch and self.active:
                # Work admitted under an older grant is still running: a new grant would overlap it.
                raise StaleLease(f"{self.active} request(s) of epoch {self.epoch} still active; drain first")
            self.epoch, self.admitting = epoch, True
        return self.info()

    def check(self, epoch: int | None) -> None:
        with self._cond:
            self._check(epoch)

    def _check(self, epoch: int | None) -> None:
        if epoch is None or epoch != self.epoch or not self.admitting:
            raise StaleLease(f"lease epoch {epoch} is not the admitted epoch {self.epoch} "
                             f"({'admitting' if self.admitting else 'not admitting'})")

    def enter(self, epoch: int | None) -> None:
        """Count one unit of GPU work (queued or running). Pair with leave()."""
        with self._cond:
            self._check(epoch)
            self.active += 1

    def leave(self) -> None:
        with self._cond:
            self.active -= 1
            self._cond.notify_all()

    @contextmanager
    def activity(self, epoch: int | None) -> Iterator[None]:
        self.enter(epoch)
        try:
            yield
        finally:
            self.leave()

    def drain(self, epoch: int, timeout: float) -> bool:
        """Stop admitting, then wait for zero activity. Check + stop happen under one lock, so no request can slip
        in between the activity check and the release acknowledgement."""
        with self._cond:
            if epoch < self.epoch:
                raise StaleLease(f"epoch {epoch} is older than {self.epoch}")
            self.epoch, self.admitting = epoch, False
            return self._cond.wait_for(lambda: self.active == 0, timeout=timeout)
