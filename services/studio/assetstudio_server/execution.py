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

__all__ = ["DirectBackend", "ExecutionBackend", "NodeBackend"]


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

    def task_finished(self, task_id: str, state: str) -> None: ...


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

    def task_finished(self, task_id: str, state: str) -> None:
        """Direct mode has no attempt custody to settle."""


class NodeBackend:
    """Engine access over runner attempts: adapters are bound per task (`env`), admission is runner-local."""

    mode = "nodes"

    def __init__(self, studio: Studio) -> None:
        self._studio = studio

    @property
    def simulated(self) -> bool:
        """Readiness view only: every fresh runner declares itself simulated. Results stay labelled per manifest."""
        from .services.node_readiness import nodes_simulated

        return nodes_simulated(self._studio)

    def engine(self, env: TaskEnv | None = None) -> ImageEngine | None:
        from .remote.image import RemoteImageEngine

        return RemoteImageEngine(self._studio, env)

    def aux(self, env: TaskEnv | None = None) -> AuxService | None:
        from .remote.aux import RemoteAux

        return RemoteAux(self._studio, env)

    def worker3d(self, env: TaskEnv | None = None) -> Worker3dService | None:
        from .remote.worker3d import RemoteWorker3d

        return RemoteWorker3d(self._studio, env)

    def acquire(self, lane: str, worker: str) -> int:
        return 0  # the runner admits GPU work itself; Studio holds no GPU lease

    def generation(self, env: TaskEnv, call_key: str) -> int:
        latest = self._studio.journal.attempts.latest(env.task.id, call_key)
        return latest["generation"] if latest is not None else 1

    def cancel_orphans(self, orphans: list[dict[str, Any]]) -> None:
        """Tasks cancelled while Studio was down: their unfinished attempts are cancelled, results disposed."""
        from .services import attempts
        from .services._runner_util import NON_TERMINAL

        for t in orphans:
            for a in self._studio.journal.attempts.list(task_id=t["id"], states=NON_TERMINAL):
                if a["state"] == "ingested":
                    attempts.dispose(self._studio, a["id"], "cancelled")
                else:
                    attempts.cancel(self._studio, a["id"])

    def task_finished(self, task_id: str, state: str) -> None:
        """R8: results of a succeeded task are committed; a failed or cancelled task's results are disposed."""
        from .services import attempts

        for a in self._studio.journal.attempts.list(task_id=task_id, states=("ingested",)):
            if state == "succeeded":
                attempts.commit(self._studio, a["id"])
            elif state in ("failed", "cancelled"):
                attempts.dispose(self._studio, a["id"], "rejected" if state == "failed" else "cancelled")
