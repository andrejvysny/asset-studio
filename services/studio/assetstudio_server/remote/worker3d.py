"""Remote Worker3dService: generate/export run on a runner; the stage's ack marks the call's output committed."""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from assetstudio_protocol import calls as pc

from ..services import attempts
from ..services.node_readiness import node_exporters, operation_readiness
from .calls import result_simulated, run_call

if TYPE_CHECKING:
    from ..coordinator.runner import TaskEnv
    from ..studio import Studio

__all__ = ["RemoteWorker3d"]

_STATUS = {"offered": "pending", "leased": "running", "admitted": "running", "executing": "running",
           "spooled": "running", "uploading": "running", "uncertain": "running", "ingested": "succeeded",
           "committed": "succeeded", "failed": "failed", "lost": "lost", "cancelled": "cancelled",
           "quarantined": "lost"}


class RemoteWorker3d:
    name = "worker3d"

    def __init__(self, studio: Studio, env: TaskEnv | None) -> None:
        self.studio, self.env = studio, env

    @property
    def simulated(self) -> bool:
        return self.env is not None and result_simulated(self.studio, self.env.task.id)

    def _attempt(self, execution_id: str) -> dict[str, Any] | None:
        """The task's attempt carrying this execution id (adapters are per access, so nothing is cached)."""
        if self.env is None:
            return None
        found = [a for a in self.studio.journal.attempts.list(task_id=self.env.task.id)
                 if a["offer"]["params"].get("execution_id") == execution_id]
        return max(found, key=lambda a: a["generation"], default=None)

    def status(self, execution_id: str) -> dict[str, Any] | None:
        a = self._attempt(execution_id)
        return None if a is None else {"state": _STATUS[a["state"]], "attempt_id": a["id"]}

    def execute(self, execution_id: str, op: str, params: dict[str, Any], body: bytes, *, epoch: int,
                should_cancel: Callable[[], bool] | None = None) -> tuple[bytes, dict[str, Any]]:
        assert self.env is not None, "an unbound RemoteWorker3d cannot run calls"
        wire = pc.Worker3dParams(execution_id=execution_id, op=op, params=params)  # type: ignore[arg-type]
        mime = "application/octet-stream"
        files, meta = run_call(self.studio, self.env, f"worker3d.{op}", wire, [(body, "body", "", mime)],
                               should_cancel=should_cancel)
        return files["result.bin"], {**meta, "simulated": bool(meta.get("simulated"))}

    def ack(self, execution_id: str) -> None:
        """The artifact is registered: R8 'committed' for this call (a no-op once committed)."""
        a = self._attempt(execution_id)
        if a is not None:
            attempts.commit(self.studio, a["id"])

    def health(self) -> dict[str, Any]:
        gen, why_gen = operation_readiness(self.studio, "worker3d.generate")
        exp, why_exp = operation_readiness(self.studio, "worker3d.export")
        ok = gen and exp
        return {"reachable": ok, "ok": ok, "exporters": node_exporters(self.studio), "loads": None, "loaded": None,
                "problems": [] if ok else (why_gen if not gen else why_exp)}

    def lease(self, epoch: int) -> dict[str, Any]:
        return {"leased": False, "reason": "runner-local admission"}

    def unload(self, owner_token: str, epoch: int) -> dict[str, Any]:
        return {"loaded": False, "reason": "runner-local admission"}
