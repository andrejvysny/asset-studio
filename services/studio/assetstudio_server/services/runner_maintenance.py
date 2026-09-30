"""Periodic runner upkeep: lease expiry, re-placement of unplaced offers, upload expiry (R5, R6, R9)."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from ..studio import Studio
from ..taskstore import INFLIGHT_ATTEMPTS
from . import attempts, transfers
from ._runner_util import NON_TERMINAL

log = logging.getLogger("assetstudio")


def reconcile_tasks(studio: Studio) -> int:
    """R8: a `reconciling` task waits for its runner attempts. Once none is in flight it is requeued (the stage
    re-enters and replays ingested results); a pending cancel completes only when the runner has acknowledged it."""
    store, settled = studio.journal, 0
    for t in store.tasks.list(states=("reconciling",)):
        if t.control == "cancel_requested":
            for a in store.attempts.list(task_id=t.id, states=NON_TERMINAL):
                if a["state"] == "ingested":
                    attempts.dispose(studio, a["id"], "cancelled")
                elif a["control"] != "cancel":
                    attempts.cancel(studio, a["id"])
        if store.attempts.list(task_id=t.id, states=INFLIGHT_ATTEMPTS, limit=1):
            continue
        settled += store.tasks.settle_reconciling(t.id, "cancelled" if t.control == "cancel_requested" else "queued")
    return settled


class RunnerMaintenance:
    def __init__(self, studio: Studio) -> None:
        self.studio = studio
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> None:
        steps: tuple[tuple[str, Callable[[Studio], object]], ...] = (
            ("expire attempts", attempts.expire), ("place pending offers", attempts.place_pending),
            ("expire uploads", transfers.expire_uploads), ("reconcile tasks", reconcile_tasks))
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
