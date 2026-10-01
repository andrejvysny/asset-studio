"""Image operations against a ComfyUI-style engine: reconcile by prompt id, poll, cancel, fetch (mirrors Studio's
generate stage, which this replaces on the runner)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from assetstudio_protocol import calls
from assetstudio_protocol import engine as eng

from .engines.comfyui import graph_sha256
from .executor import ExecutionCancelled, ExecutionFailed

_PASS_CODES = ("input_invalid", "oom")


@dataclass(frozen=True)
class Poll:
    sleep: Callable[[float], None]
    clock: Callable[[], float]
    interval_s: float
    timeout_s: float


def workflow_receipt(engine: Any, edit: bool) -> dict[str, Any]:
    """The workflow a generation used, from the engine's registry when it has one (else its description)."""
    registry = getattr(engine, "registry", None)
    if registry is not None:
        found = registry.by_kind("image_edit" if edit else "t2i")
        if found:
            wf = found[0]
            return {"id": wf.id, "version": wf.version, "graph_sha256": graph_sha256(wf.graph)}
    d = engine.describe()
    return {"id": d.get("workflow"), "version": d.get("workflow_version"), "graph_sha256": None}


def _await(engine: Any, prompt_id: str, should_cancel: Callable[[], bool], poll: Poll) -> None:
    deadline = poll.clock() + poll.timeout_s
    while True:
        if should_cancel():
            engine.cancel(prompt_id)
            raise ExecutionCancelled()
        st = engine.status(prompt_id)
        if st.state == "succeeded":
            return
        if st.state == "failed":
            code = st.detail.get("code")
            raise ExecutionFailed(code if code in _PASS_CODES else "internal", st.error or "generation failed")
        if st.state == "unknown":
            raise ExecutionFailed("internal", "engine lost the job")
        if poll.clock() > deadline:
            engine.cancel(prompt_id)
            raise ExecutionFailed("internal", "generation timed out")
        poll.sleep(poll.interval_s)


def _edit_workflow_id(engine: Any, receipt: dict[str, Any] | None) -> str | None:
    """A crash between submit and the receipt leaves none: the output node must still come from the edit workflow."""
    if receipt and receipt.get("workflow"):
        return str(receipt["workflow"])
    registry = getattr(engine, "registry", None)
    found = registry.by_kind("image_edit") if registry is not None else []
    return found[0].id if found else None


def run_t2i(engine: Any, p: calls.T2IParams, should_cancel: Callable[[], bool],
            poll: Poll) -> tuple[bytes, dict[str, Any]]:
    req = eng.T2IRequest(**p.model_dump(exclude={"style_lora", "speed_lora"}),
                         style_lora=_lora(p.style_lora), speed_lora=_lora(p.speed_lora))
    if engine.status(req.prompt_id).state == "unknown":
        engine.submit(req)
    _await(engine, req.prompt_id, should_cancel, poll)
    meta = {"engine": engine.describe(), "workflow": workflow_receipt(engine, False), "prompt_id": req.prompt_id,
            "simulated": engine.simulated}
    return engine.fetch_image(req.prompt_id), meta


def run_edit(engine: Any, p: calls.EditParams, image: bytes, should_cancel: Callable[[], bool],
             poll: Poll) -> tuple[bytes, dict[str, Any]]:
    req = eng.ImageEditRequest(**p.model_dump(), image=image)
    receipt: dict[str, Any] | None = None
    if engine.status(req.prompt_id).state == "unknown":
        receipt = engine.submit_edit(req)
    _await(engine, req.prompt_id, should_cancel, poll)
    workflow = workflow_receipt(engine, True)
    if receipt and getattr(engine, "registry", None) is None:  # registry-less engines describe only their t2i flow
        workflow = {"id": receipt["workflow"], "version": receipt["workflow_version"],
                    "graph_sha256": receipt["graph_sha256"]}
    meta = {"engine": engine.describe(), "workflow": workflow, "prompt_id": req.prompt_id,
            "simulated": engine.simulated, "receipt": receipt}
    return engine.fetch_image(req.prompt_id, _edit_workflow_id(engine, receipt)), meta


def _lora(p: calls.LoraParam | None) -> eng.LoraUse | None:
    return eng.LoraUse(p.file, p.strength) if p else None
