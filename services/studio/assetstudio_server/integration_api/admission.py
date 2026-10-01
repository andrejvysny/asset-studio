"""Aggregate resource admission for publication: staging-space reservations, a bounded processing queue on a dedicated
thread limiter, a per-credential pending-preview cap and the set of previews the sweeper must not delete."""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread

from ..services import source_publications as sp
from ..services.principals import ServiceError
from ..settings import Settings

SCAN_TTL = 2.0


def _busy(reason: str, message: str) -> ServiceError:
    return ServiceError("temporarily_unavailable", message, retryable=True, details={"reason": reason})


class Admission:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.staging_max = settings.integration_staging_max_bytes
        self.floor = settings.integration_disk_floor_bytes
        self.queue_max = settings.integration_queue_max
        self.per_credential = settings.integration_previews_per_token
        self.slots = settings.integration_processing_slots
        # anyio limiters belong to one event loop; one per loop (uvicorn serves all listeners on a single loop).
        self._limiters: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, anyio.CapacityLimiter] = (
            weakref.WeakKeyDictionary())
        self.active_previews: set[str] = set()
        self._lock = threading.Lock()
        self._reserved = 0
        self._inflight = 0
        self._scan: tuple[float, int] = (float("-inf"), 0)

    @property
    def processing(self) -> anyio.CapacityLimiter:
        loop = asyncio.get_running_loop()
        with self._lock:
            if loop not in self._limiters:
                self._limiters[loop] = anyio.CapacityLimiter(self.slots)
            return self._limiters[loop]

    @property
    def reserved(self) -> int:
        with self._lock:
            return self._reserved

    def _staging_used(self, root: Path) -> int:
        stamp, used = self._scan
        if time.monotonic() - stamp <= SCAN_TTL:
            return used
        used = 0
        for d in root.glob("ipv_*"):
            for f in d.rglob("*"):
                try:
                    used += f.stat().st_size if f.is_file() else 0
                except OSError:
                    continue  # raced with a discard
        self._scan = (time.monotonic(), used)
        return used

    def _free(self, path: Path) -> int:
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return shutil.disk_usage(probe).free

    @contextmanager
    def reserve(self, nbytes: int) -> Iterator[None]:
        root = sp.staging_root(self.settings)
        with self._lock:
            if self._staging_used(root) + self._reserved + nbytes > self.staging_max:
                raise _busy("staging_capacity", "staging capacity exhausted; retry later")
            for where in (root, Path(tempfile.gettempdir())):
                if self._free(where) - self._reserved - nbytes < self.floor:
                    raise _busy("disk_floor", "staging capacity exhausted; retry later")
            self._reserved += nbytes
        try:
            yield
        finally:
            with self._lock:
                self._reserved -= nbytes

    async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Heavy work on a dedicated limiter, so it never occupies the default pool shared with other endpoints. The
        queue bound is process-wide (running + waiting), independent of which event loop serves the request."""
        with self._lock:
            if self._inflight >= self.slots + self.queue_max:
                raise _busy("processing_queue_full", "server is busy processing other publications; retry later")
            self._inflight += 1
        try:
            return await anyio.to_thread.run_sync(fn, *args, limiter=self.processing)
        finally:
            with self._lock:
                self._inflight -= 1

    @contextmanager
    def hold(self, preview_id: str) -> Iterator[None]:
        with self._lock:
            self.active_previews.add(preview_id)
        try:
            yield
        finally:
            with self._lock:
                self.active_previews.discard(preview_id)

    def active_snapshot(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self.active_previews)

    def check_preview_quota(self, credential_id: str) -> None:
        root, now, pending = sp.staging_root(self.settings), sp._now(), 0
        if not root.is_dir():
            return
        for d in root.glob("ipv_*"):
            receipt = sp._read_receipt(d)
            if receipt is not None and receipt.get("credential_id") == credential_id and not sp._expired(d, now):
                pending += 1
        if pending >= self.per_credential:
            raise _busy("preview_quota", "too many pending previews for this token; commit or let them expire")
