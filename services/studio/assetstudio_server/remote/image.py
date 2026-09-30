"""Remote ImageEngine: submit/submit_edit run the whole call on a runner and block; status/fetch serve the result.

Stages poll `status` after `submit` and re-enter with a live attempt after a Studio restart, so the adapter answers
from its in-memory result first and from the attempt journal otherwise.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from assetstudio_protocol import calls as protocol_calls

from ..adapters.base import ExecutionFailed, ImageEditRequest, JobStatus, T2IRequest
from ..adapters.comfyui import WorkflowRegistry, graph_sha256
from ..services import attempts
from .calls import image_mime, read_result, result_simulated, run_stage_call

if TYPE_CHECKING:
    from ..coordinator.runner import TaskEnv
    from ..studio import Studio

__all__ = ["RemoteImageEngine", "workflow_registry"]

_RUNNING = ("offered", "leased", "admitted", "executing", "spooled", "uploading")
_RESUBMIT = ("lost", "uncertain", "quarantined", "cancelled")  # the next submit re-enters run_call, which decides
_RETRYABLE = ("node_unavailable", "admission_rejected", "uncertain_execution")


def workflow_registry(studio: Studio) -> WorkflowRegistry:
    """Studio's own copy of the versioned workflows (loaded once): the pinned graph the runners must match."""
    reg = studio.extras.get("node_workflows")
    if reg is None:
        reg = studio.extras.setdefault("node_workflows", WorkflowRegistry(studio.settings.workflows_dir))
    return reg


class RemoteImageEngine:
    name = "remote-comfyui"

    def __init__(self, studio: Studio, env: TaskEnv | None) -> None:
        self.studio, self.env = studio, env
        self.registry = workflow_registry(studio)
        self._results: dict[str, tuple[bytes, dict[str, Any]]] = {}

    @property
    def simulated(self) -> bool:
        return self.env is not None and result_simulated(self.studio, self.env.task.id)

    def check(self) -> dict[str, Any]:
        return {"reachable": True, "ready": True, "problems": []}  # runner readiness arrives with WP2.6

    def supports(self, kind: str) -> bool:
        return bool(self.registry.by_kind(kind))

    def describe(self) -> dict[str, Any]:
        found = self.registry.by_kind("t2i")
        return {"engine": self.name, "workflow": found[0].id if found else None,
                "workflow_version": found[0].version if found else None}

    # --- submit -------------------------------------------------------------------------------------------
    def _run(self, operation: str, params: Any, prompt_id: str, kind: str,
             inputs: list[tuple[bytes, str, str, str]]) -> dict[str, Any]:
        assert self.env is not None
        files, meta = run_stage_call(self.studio, self.env, operation, params, inputs)
        self._check_workflow(kind, meta)
        self._results[prompt_id] = (files["image.png"], meta)
        return meta

    def _check_workflow(self, kind: str, meta: dict[str, Any]) -> None:
        found = self.registry.by_kind(kind)
        theirs = (meta.get("workflow") or {}).get("graph_sha256")
        if found and theirs and theirs != graph_sha256(found[0].graph):
            raise ExecutionFailed("runner workflow differs from Studio's pinned workflow", "input_invalid")

    def submit(self, req: T2IRequest) -> str:
        self._run("image.t2i", protocol_calls.T2IParams.model_validate(asdict(req)), req.prompt_id, "t2i", [])
        return req.prompt_id

    def submit_edit(self, req: ImageEditRequest) -> dict[str, Any]:
        params = protocol_calls.EditParams.model_validate({k: v for k, v in asdict(req).items() if k != "image"})
        meta = self._run("image.edit", params, req.prompt_id, "image_edit",
                         [(req.image, "image", "", image_mime(req.image))])
        if meta.get("receipt"):
            return dict(meta["receipt"])
        wf = meta.get("workflow") or {}
        return {"prompt_id": req.prompt_id, "workflow": wf.get("id"), "workflow_version": wf.get("version"),
                "graph_sha256": wf.get("graph_sha256"), "input": {"sha256": req.prepared_input_sha256}}

    # --- status / fetch / cancel --------------------------------------------------------------------------
    def _attempt(self, prompt_id: str) -> dict[str, Any] | None:
        if self.env is None or self.env.current_call is None:
            return None
        a = self.studio.journal.attempts.latest(self.env.task.id, self.env.current_call)
        return a if a is not None and a["offer"]["params"].get("prompt_id") == prompt_id else None

    def status(self, prompt_id: str) -> JobStatus:
        if prompt_id in self._results:
            return JobStatus("succeeded")
        a = self._attempt(prompt_id)
        if a is None or a["state"] in _RESUBMIT:
            return JobStatus("unknown")
        if a["state"] in _RUNNING:
            return JobStatus("running")
        if a["state"] == "failed":
            err = a["error"] or {}
            if err.get("code") in _RETRYABLE or a["disposition"] == "rejected":
                return JobStatus("unknown")
            return JobStatus("failed", err.get("message") or "attempt failed", {"code": err.get("code")})
        return JobStatus("succeeded")

    def fetch_image(self, prompt_id: str, workflow_id: str | None = None) -> bytes:
        if prompt_id in self._results:
            return self._results[prompt_id][0]
        a = self._attempt(prompt_id)
        if a is None or a["state"] not in ("ingested", "committed"):
            raise ExecutionFailed(f"no result for prompt {prompt_id}", "internal")
        assert self.env is not None
        return read_result(self.env, a)[0]["image.png"]

    def cancel(self, prompt_id: str) -> dict[str, Any]:
        a = self._attempt(prompt_id)
        return {"requested": a is not None and attempts.cancel(self.studio, a["id"])}
