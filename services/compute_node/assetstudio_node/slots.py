"""Slot supervisor and bounded transfer pool for the runner agent (H11).

A slot runs at most one attempt; a physical device is held by at most one running attempt, so slots that share a
device can never run heavy work together. Result uploads run on a small pool so a slot is free again as soon as
its result is spooled."""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable

from .config import RunnerConfig

log = logging.getLogger("assetstudio_node")


class SlotSupervisor:
    def __init__(self, config: RunnerConfig) -> None:
        self._devices = {s.slot_id: frozenset(s.devices) for s in config.slots}
        self._order = [s.slot_id for s in config.slots]
        self._lock = threading.Lock()
        self._busy: dict[str, str] = {}
        self._held: set[str] = set()
        self._workers = config.transfer_workers
        self._jobs: queue.Queue[tuple[str, Callable[[], None]] | None] = queue.Queue()
        self._pool: list[threading.Thread] = []
        self._inflight: set[str] = set()
        self.errors: queue.Queue[BaseException] = queue.Queue()  # for the main loop: backoff, re-session

    # -- slots ---------------------------------------------------------------------------------------------------

    def claim(self, slot_id: str, attempt_id: str) -> bool:
        devices = self._devices.get(slot_id, frozenset())
        with self._lock:
            if slot_id in self._busy or devices & self._held:
                return False
            self._busy[slot_id] = attempt_id
            self._held |= devices
            return True

    def release(self, slot_id: str) -> None:
        with self._lock:
            self._busy.pop(slot_id, None)
            self._held -= self._devices.get(slot_id, frozenset())

    def any_busy(self) -> bool:
        with self._lock:
            return bool(self._busy)

    def idle_slots(self, states: dict[str, str]) -> list[str]:
        """Barrier-ready slots with no running attempt and no device held by another slot."""
        with self._lock:
            return [s for s in self._order if states.get(s, "ready") == "ready" and s not in self._busy
                    and not self._devices[s] & self._held]

    def spawn(self, slot_id: str, work: Callable[[], None]) -> None:
        """Run `work` for an already claimed slot on its own thread; the claim is released when it ends."""
        def run() -> None:
            try:
                work()
            except Exception as e:  # noqa: BLE001 - surfaced to the main loop, never lost with the thread
                self.errors.put(e)
            finally:
                self.release(slot_id)

        threading.Thread(target=run, name=f"runner-slot-{slot_id}", daemon=True).start()

    # -- transfers -----------------------------------------------------------------------------------------------

    def submit_transfer(self, attempt_id: str, job: Callable[[], None]) -> bool:
        """Queue a delivery; False when this attempt is already queued or uploading."""
        with self._lock:
            if attempt_id in self._inflight:
                return False
            self._inflight.add(attempt_id)
            while len(self._pool) < self._workers:
                t = threading.Thread(target=self._transfer_loop, name=f"runner-transfer-{len(self._pool)}",
                                     daemon=True)
                self._pool.append(t)
                t.start()
        self._jobs.put((attempt_id, job))
        return True

    def _transfer_loop(self) -> None:
        while (item := self._jobs.get()) is not None:
            attempt_id, job = item
            try:
                job()
            except Exception as e:  # noqa: BLE001
                self.errors.put(e)
            finally:
                with self._lock:
                    self._inflight.discard(attempt_id)

    # -- lifecycle -----------------------------------------------------------------------------------------------

    def shutdown(self, timeout: float) -> bool:
        """Wait up to `timeout` s for running attempts and queued transfers; True if everything finished. Unfinished
        attempts keep their local state and are recovered on the next start."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._busy and not self._inflight:
                    break
            time.sleep(0.02)
        with self._lock:
            done = not self._busy and not self._inflight
            pool = list(self._pool)
        if not done:
            log.warning("shutdown: attempts still running after %.0fs; local state kept for recovery", timeout)
            return False
        for _ in pool:
            self._jobs.put(None)
        return True
