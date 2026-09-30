"""Periodic runner upkeep: lease expiry, re-placement of unplaced offers, upload expiry (R5, R6, R9)."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from ..studio import Studio
from . import attempts, transfers

log = logging.getLogger("assetstudio")


class RunnerMaintenance:
    def __init__(self, studio: Studio) -> None:
        self.studio = studio
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> None:
        steps: tuple[tuple[str, Callable[[Studio], object]], ...] = (
            ("expire attempts", attempts.expire), ("place pending offers", attempts.place_pending),
            ("expire uploads", transfers.expire_uploads))
        for name, step in steps:
            try:
                step(self.studio)
            except Exception:  # one failing step must not starve the others
                log.exception("runner maintenance: %s failed", name)

    def _loop(self) -> None:
        while not self._stop.wait(self.studio.settings.runner_maintenance_s):
            self.run_once()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="runner-maintenance", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
