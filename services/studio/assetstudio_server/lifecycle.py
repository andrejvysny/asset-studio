"""Shutdown coordination: shared Studio state may close only after every tracked mutation thread has finished."""
from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator


class GateClosed(Exception):
    pass


class MutationGate:
    """Counts in-flight mutations. Entered inside the worker thread so tracking outlives a cancelled request task."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._active = 0
        self._closed = False

    @property
    def active(self) -> int:
        with self._cond:
            return self._active

    @contextlib.contextmanager
    def enter(self) -> Iterator[None]:
        with self._cond:
            if self._closed:
                raise GateClosed("mutation gate is closed")
            self._active += 1
        try:
            yield
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify_all()

    def close_and_wait(self, timeout: float) -> bool:
        """Refuse new entries, then wait for running ones; False if they did not finish within `timeout`."""
        deadline = time.monotonic() + timeout
        with self._cond:
            self._closed = True
            while self._active:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
            return True
