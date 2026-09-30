"""Node-mode worker pools (docs/modular/compute-runner.md R8): a continuation is a Studio task worker, every engine
call is placed independently, so a capability class gets as many workers as it has eligible slots (bounded)."""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from assetstudio_protocol.execution import OPERATION_CAPABILITY

from ..services.placement import slot_problem

if TYPE_CHECKING:
    from ..studio import Studio
    from .runner import Coordinator

__all__ = ["CLASS_OF_LANE", "NodePool", "eligible_slot_count", "worker_key"]

CLASS_OF_LANE = {"gpu0": "image", "gpu1": "aux3d"}


def worker_key(lane: str, k: int | None) -> str:
    """Direct mode (k is None) keeps the plain lane name; node workers are `<lane>#<k>`."""
    return lane if k is None else f"{lane}#{k}"


def eligible_slot_count(studio: Studio, capability: str) -> int:
    """Fresh runners' slots that could serve some operation of `capability` (occupancy ignored)."""
    from ..services.node_readiness import fresh_inventories

    ops = [op for op, cap in OPERATION_CAPABILITY.items() if cap == capability]
    count = 0
    for f in fresh_inventories(studio):
        allowed = f.group["operations"]
        devices = {d["uuid"]: d for d in studio.journal.runners.devices(f.runner_id)}
        for slot in studio.journal.runners.slots(f.runner_id):
            if any((allowed == "*" or op in allowed) and slot_problem(slot, devices, set(), op, transient=False) is None
                   for op in ops):
                count += 1
    return count


class NodePool:
    def __init__(self, coordinator: Coordinator) -> None:
        self._c = coordinator
        self._lock = threading.Lock()
        self._live: dict[str, set[int]] = {lane: set() for lane in CLASS_OF_LANE}
        self._target: dict[str, int] = {lane: 1 for lane in CLASS_OF_LANE}

    def target(self, lane: str) -> int:
        return self._target[lane]

    def keys(self, lane: str) -> list[str]:
        with self._lock:
            return [worker_key(lane, k) for k in sorted(self._live[lane])]

    def refresh(self) -> None:
        """Recompute targets from the fleet and start missing workers. Never stops a thread: surplus workers leave
        at their next idle check (`should_exit`), so a busy call is never interrupted."""
        cap = max(1, self._c.studio.settings.node_workers_max)
        for lane, capability in CLASS_OF_LANE.items():
            target = min(cap, max(1, eligible_slot_count(self._c.studio, capability)))
            with self._lock:
                self._target[lane] = target
                live = self._live[lane]
                spawn = [k for k in range(cap) if k not in live][: max(0, target - len(live))]
                live.update(spawn)
            for k in spawn:
                self._c.spawn_worker(lane, k)

    def should_exit(self, lane: str, k: int) -> bool:
        with self._lock:
            live = self._live[lane]
            if k == 0 or len(live) <= self._target[lane]:
                return False
            live.discard(k)
            return True

    def left(self, lane: str, k: int) -> None:
        with self._lock:
            self._live[lane].discard(k)
