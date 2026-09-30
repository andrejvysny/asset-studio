"""Execution seam: stage and service code reach engines, aux/3D workers and GPU ownership only through here.

`DirectBackend` wraps the in-process singletons of `Studio`; node mode (remote adapters) plugs in behind the same
protocol. Attributes are read on every call because tests and reconfiguration reassign them at runtime.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

from .adapters.base import AuxService, ImageEngine, Worker3dService

if TYPE_CHECKING:
    from .coordinator.runner import TaskEnv
    from .studio import Studio

log = logging.getLogger("assetstudio.coordinator")

__all__ = ["DirectBackend", "ExecutionBackend"]


class ExecutionBackend(Protocol):
    mode: str

    @property
    def simulated(self) -> bool: ...

    def engine(self, env: TaskEnv | None = None) -> ImageEngine | None: ...

    def aux(self, env: TaskEnv | None = None) -> AuxService | None: ...

    def worker3d(self, env: TaskEnv | None = None) -> Worker3dService | None: ...

    def acquire(self, lane: str, worker: str) -> int: ...

    def generation(self, env: TaskEnv, call_key: str) -> int: ...

    def cancel_orphans(self, tasks: list[dict[str, Any]]) -> None: ...


class DirectBackend:
    mode = "direct"

    def __init__(self, studio: Studio) -> None:
        self._studio = studio

    @property
    def simulated(self) -> bool:
        return self._studio.simulated

    def engine(self, env: TaskEnv | None = None) -> ImageEngine | None:
        return self._studio.engine

    def aux(self, env: TaskEnv | None = None) -> AuxService | None:
        return self._studio.aux

    def worker3d(self, env: TaskEnv | None = None) -> Worker3dService | None:
        return self._studio.worker3d

    def acquire(self, lane: str, worker: str) -> int:
        return self._studio.lanes[lane].acquire(worker)

    def generation(self, env: TaskEnv, call_key: str) -> int:
        return 1  # direct mode never re-places a call: ids keep their legacy form

    def cancel_orphans(self, orphans: list[dict[str, Any]]) -> None:
        """Cancelled-while-down generation tasks: stop their own unfinished engine prompts by exact id."""
        engine = self._studio.engine
        if engine is None:
            return
        for t in orphans:
            if t["stage"] != "generate":
                continue
            for slot in (t["progress"].get("engine") or {}).values():
                if isinstance(slot, dict) and slot.get("prompt_id") and "artifact_id" not in slot:
                    try:
                        engine.cancel(slot["prompt_id"])
                    except Exception as e:  # noqa: BLE001 - unknown outcome: logged, the prompt id stays recorded
                        log.warning("cancel of orphaned prompt %s (task %s) not confirmed: %s",
                                    slot["prompt_id"], t["id"], e)
