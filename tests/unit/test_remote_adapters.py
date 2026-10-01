"""run_call outcome mapping, re-entry rules and the workflow pin check of the remote adapters (no runner: attempt
states are driven directly in the journal). Contract evidence only."""
from __future__ import annotations

import hashlib
import io
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

import pytest
from assetstudio_protocol import calls as pc
from assetstudio_server.adapters.base import (
    EngineUnavailable,
    ExecutionCancelled,
    ExecutionFailed,
    ExecutionLost,
    ImageEditRequest,
    T2IRequest,
)
from assetstudio_server.adapters.comfyui import graph_sha256
from assetstudio_server.coordinator.errors import Cancelled
from assetstudio_server.coordinator.runner import TaskEnv
from assetstudio_server.execution import NodeBackend
from assetstudio_server.remote.calls import run_call, run_stage_call
from assetstudio_server.remote.image import RemoteImageEngine
from assetstudio_server.taskstore import NewTask

from tests.conftest import Api, new_project

PARAMS = pc.AuxCutoutParams()
INPUTS = [(b"png-bytes", "image", "", "image/png")]


class Rig:
    def __init__(self, api: Api) -> None:
        self.api, self.studio = api, api.studio
        self.pid = new_project(api)
        t = NewTask(project_id=self.pid, job_id=f"job_{1:0>16}", item_id=f"itm_{1:0>16}", stage="generate",
                    family="generate", input_key="1", inputs={}, lane="gpu1", residency="x")
        self.studio.journal.tasks.create([t], "cmd_x")
        assert self.studio.journal.tasks.claim(t.id, "pas_x")
        self.task_id = t.id
        self.env = self.make_env()

    def make_env(self) -> TaskEnv:
        task = self.studio.journal.tasks.get(self.task_id)
        assert task is not None
        return TaskEnv(self.studio, self.studio.registry.get(self.pid), task)

    def latest(self, key: str = "aux.cutout") -> dict[str, Any] | None:
        return self.studio.journal.attempts.latest(self.task_id, key)

    def store_blob(self, data: bytes) -> dict[str, Any]:
        sha = hashlib.sha256(data).hexdigest()
        self.env.ctx.store.repo.write_blob(io.BytesIO(data), expected_sha256=sha)
        return {"name": "result.bin", "sha256": sha, "size": len(data), "mime": "application/octet-stream"}

    def drive(self, mutate: Callable[[dict[str, Any]], None], call: Callable[[], Any], *, key: str = "aux.cutout",
              generation: int = 1) -> Any:
        """Run `call` (blocking) on a thread, apply `mutate` to the attempt once it exists; returns result/exception."""
        out: list[Any] = []

        def go() -> None:
            try:
                out.append(call())
            except BaseException as e:  # noqa: BLE001 - handed back to the test
                out.append(e)
        t = threading.Thread(target=go)
        t.start()
        end = time.monotonic() + 5
        while time.monotonic() < end:
            a = self.latest(key)
            if a is not None and a["generation"] == generation:
                mutate(a)
                break
            time.sleep(0.01)
        t.join(10)
        assert not t.is_alive() and out
        return out[0]

    def set_state(self, state: str, **cols: Any) -> Callable[[dict[str, Any]], None]:
        def apply(a: dict[str, Any]) -> None:
            assert self.studio.journal.attempts.transition(a["id"], ("offered",), state, **cols)
        return apply

    def call(self, wait_s: float = 5.0, **kw: Any) -> Any:
        return run_call(self.studio, self.env, "aux.cutout", PARAMS, INPUTS, wait_s=wait_s, **kw)


@pytest.fixture
def rig(make_api: Callable[..., Api]) -> Rig:
    return Rig(make_api(coordinator=False))


def fail(code: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": f"runner said {code}"}}


@pytest.mark.parametrize(("code", "exc", "attr"), [
    ("invalid_input", ExecutionFailed, "input_invalid"),
    ("validation_failed", ExecutionFailed, "input_invalid"),
    ("resource_exhausted", ExecutionFailed, "oom"),
    ("node_unavailable", EngineUnavailable, None),
    ("admission_rejected", EngineUnavailable, None),
    ("uncertain_execution", ExecutionLost, None),
    ("stale_generation", ExecutionFailed, "internal"),
])
def test_failed_attempt_code_maps_to_engine_exception(rig: Rig, code: str, exc: type, attr: str | None) -> None:
    e = rig.drive(rig.set_state("failed", **fail(code)), rig.call)
    assert type(e) is exc and f"runner said {code}" in str(e)
    if attr is not None:
        assert e.code == attr


def test_success_returns_files_and_meta_and_records_progress(rig: Rig) -> None:
    ref = rig.store_blob(b"the result")
    manifest = {"files": [ref], "meta": {"simulated": True}}
    files, meta = rig.drive(rig.set_state("ingested", manifest=manifest), rig.call)
    assert files == {"result.bin": b"the result"} and meta == {"simulated": True}
    a = rig.latest()
    assert a is not None
    assert rig.make_env().task.progress["remote"]["aux.cutout"]["attempt_id"] == a["id"]
    assert rig.make_env().task.progress["remote"]["aux.cutout"]["generation"] == 1


def test_remote_progress_keeps_other_keys(rig: Rig) -> None:
    rig.env.progress(done=3)
    rig.drive(rig.set_state("failed", **fail("invalid_input")), rig.call)
    progress = rig.make_env().task.progress
    assert progress["done"] == 3 and "aux.cutout" in progress["remote"]


def test_cancelled_and_quarantined_attempts(rig: Rig) -> None:
    assert isinstance(rig.drive(rig.set_state("cancelled"), rig.call), ExecutionCancelled)
    # cancelled is superseded by a retried task: the re-entry opens generation 2
    e = rig.drive(rig.set_state("quarantined"), rig.call, generation=2)
    assert isinstance(e, EngineUnavailable) and "superseded" in str(e)


def test_stage_call_translates_cancel_and_lost(rig: Rig) -> None:
    def stage() -> Any:
        return run_stage_call(rig.studio, rig.env, "aux.cutout", PARAMS, INPUTS, wait_s=5)
    assert isinstance(rig.drive(rig.set_state("cancelled"), stage), Cancelled)
    assert isinstance(rig.drive(rig.set_state("lost"), stage, generation=2), EngineUnavailable)


def test_uncertain_attempt_blocks_and_is_never_replaced(rig: Rig) -> None:
    e = rig.drive(rig.set_state("uncertain"), rig.call)
    assert isinstance(e, EngineUnavailable) and "uncertain" in str(e)
    with pytest.raises(EngineUnavailable, match="uncertain"):  # re-entry by the retry loop: same attempt
        rig.call()
    rows = rig.studio.journal.attempts.for_call(rig.task_id, "aux.cutout")
    assert [(r["generation"], r["state"]) for r in rows] == [(1, "uncertain")]


@pytest.mark.parametrize("code", ["node_unavailable", "admission_rejected", "uncertain_execution"])
def test_retryable_failure_starts_next_generation_on_reentry(rig: Rig, code: str) -> None:
    assert isinstance(rig.drive(rig.set_state("failed", **fail(code)), rig.call), (EngineUnavailable, ExecutionLost))
    ref = rig.store_blob(b"second try")
    files, _ = rig.drive(rig.set_state("ingested", manifest={"files": [ref], "meta": {}}), rig.call, generation=2)
    assert files["result.bin"] == b"second try"
    rows = rig.studio.journal.attempts.for_call(rig.task_id, "aux.cutout")
    assert [(r["generation"], r["state"]) for r in rows] == [(1, "failed"), (2, "ingested")]


def test_non_retryable_failure_is_not_replaced(rig: Rig) -> None:
    rig.drive(rig.set_state("failed", **fail("invalid_input")), rig.call)
    assert isinstance(rig.drive(lambda a: None, rig.call, generation=1), ExecutionFailed)
    assert len(rig.studio.journal.attempts.for_call(rig.task_id, "aux.cutout")) == 1


def test_unplaced_offer_reports_placement_reasons(rig: Rig) -> None:
    rig.studio.settings.runner_offer_ttl_s = 0
    with pytest.raises(EngineUnavailable, match="no eligible runner"):
        rig.call()
    a = rig.latest()
    assert a is not None and a["state"] == "offered" and a["runner_id"] is None  # stays: re-entry re-places


def test_wait_budget_exceeded_while_running(rig: Rig) -> None:
    e = rig.drive(rig.set_state("executing"), lambda: rig.call(wait_s=0.5))
    assert isinstance(e, EngineUnavailable) and "wait budget" in str(e)
    a = rig.latest()
    assert a is not None and a["state"] == "executing"  # the attempt keeps running; nothing is cancelled


def test_should_cancel_cancels_the_attempt(rig: Rig) -> None:
    with pytest.raises(ExecutionCancelled):
        rig.call(should_cancel=lambda: True)
    a = rig.latest()
    assert a is not None and a["control"] == "cancel" and a["state"] == "cancelled"


def test_task_cancel_request_cancels_attempt_and_raises_cancelled(rig: Rig) -> None:
    rig.studio.journal.tasks.request_cancel([rig.task_id])
    with pytest.raises(Cancelled):
        rig.call()
    a = rig.latest()
    assert a is not None and a["control"] == "cancel"


def test_changed_inputs_for_the_same_call_are_refused(rig: Rig) -> None:
    rig.drive(rig.set_state("failed", **fail("invalid_input")), rig.call)
    with pytest.raises(ExecutionFailed, match="different inputs"):
        run_call(rig.studio, rig.env, "aux.cutout", PARAMS, [(b"other", "image", "", "image/png")], wait_s=1)


# --- RemoteImageEngine --------------------------------------------------------------------------------------------
T2I = T2IRequest(prompt_id="p1", positive="a cat", negative="", seed=7, width=64, height=64, steps=4, cfg=1.0,
                 filename_prefix="x")


def image_rig(rig: Rig) -> RemoteImageEngine:
    return RemoteImageEngine(rig.studio, rig.env)


def test_image_submit_keeps_result_and_compares_workflow_pin(rig: Rig) -> None:
    engine = image_rig(rig)
    wf = engine.registry.by_kind("t2i")[0]
    png = {"name": "image.png", "sha256": "", "size": 0, "mime": "image/png"}
    png = {**png, **{k: v for k, v in rig.store_blob(b"PNGDATA").items() if k in ("sha256", "size")}}
    meta = {"workflow": {"id": wf.id, "version": wf.version, "graph_sha256": graph_sha256(wf.graph)},
            "simulated": True}
    with rig.env.call("0"):
        rig.drive(rig.set_state("ingested", manifest={"files": [png], "meta": meta}),
                  lambda: engine.submit(T2I), key="0")
        assert engine.status("p1").state == "succeeded" and engine.fetch_image("p1") == b"PNGDATA"
        fresh = image_rig(rig)  # a new adapter (re-entry) answers from the attempt journal
        assert fresh.status("p1").state == "succeeded" and fresh.fetch_image("p1") == b"PNGDATA"
        assert fresh.status("other").state == "unknown" and fresh.simulated is True


def test_image_workflow_graph_mismatch_is_input_invalid(rig: Rig) -> None:
    engine = image_rig(rig)
    wf = engine.registry.by_kind("t2i")[0]
    png = {"name": "image.png", **{k: v for k, v in rig.store_blob(b"PNGDATA").items() if k in ("sha256", "size")},
           "mime": "image/png"}
    meta = {"workflow": {"id": wf.id, "version": wf.version, "graph_sha256": "0" * 64}}
    with rig.env.call("0"):
        e = rig.drive(rig.set_state("ingested", manifest={"files": [png], "meta": meta}),
                      lambda: engine.submit(T2I), key="0")
    assert isinstance(e, ExecutionFailed) and e.code == "input_invalid"
    assert "differs from Studio's pinned workflow" in str(e)


def test_image_status_maps_attempt_states(rig: Rig) -> None:
    engine = image_rig(rig)
    params = pc.T2IParams.model_validate(asdict(T2I))

    def submit() -> Any:
        with rig.env.call("0"):
            return run_stage_call(rig.studio, rig.env, "image.t2i", params, [], wait_s=0.3)
    with rig.env.call("0"):
        assert engine.status("p1").state == "unknown"  # no attempt yet
    assert isinstance(rig.drive(rig.set_state("executing"), submit, key="0"), EngineUnavailable)
    with rig.env.call("0"):
        assert engine.status("p1").state == "running"
        rig.studio.journal.attempts.transition(rig.latest("0")["id"], ("executing",), "failed", **fail("invalid_input"))  # type: ignore[index]
        st = engine.status("p1")
        assert st.state == "failed" and st.detail == {"code": "invalid_input"}
        rig.studio.journal.attempts.transition(rig.latest("0")["id"], ("failed",), "failed", event="x", **fail("node_unavailable"))  # type: ignore[index]
        assert engine.status("p1").state == "unknown"  # retryable: the next submit opens generation 2


def test_image_edit_receipt_falls_back_to_workflow_meta(rig: Rig) -> None:
    engine = image_rig(rig)
    wf = engine.registry.by_kind("image_edit")[0]
    png = {"name": "image.png", **{k: v for k, v in rig.store_blob(b"EDITED").items() if k in ("sha256", "size")},
           "mime": "image/png"}
    meta = {"workflow": {"id": wf.id, "version": wf.version, "graph_sha256": graph_sha256(wf.graph)},
            "receipt": None}
    req = ImageEditRequest(prompt_id="pe", source_sha256="a" * 64, prepared_input_sha256="b" * 64, image=b"\x89PNG..",
                           positive="p", negative="", seed=1, steps=2, cfg=1.0, filename_prefix="x")
    with rig.env.call("0"):
        receipt = rig.drive(rig.set_state("ingested", manifest={"files": [png], "meta": meta}),
                            lambda: engine.submit_edit(req), key="0")
    assert receipt["workflow"] == wf.id and receipt["graph_sha256"] == graph_sha256(wf.graph)


# --- NodeBackend --------------------------------------------------------------------------------------------------
def _ingested(rig: Rig, key: str) -> str:
    rig.env._calls.append(key)
    try:
        ref = rig.store_blob(key.encode())
        rig.drive(rig.set_state("ingested", manifest={"files": [ref], "meta": {}}), rig.call, key=key)
    finally:
        rig.env._calls.pop()
    return rig.studio.journal.attempts.latest(rig.task_id, key)["id"]  # type: ignore[index]


@pytest.mark.parametrize(("state", "disposition", "final"), [("succeeded", "committed", "committed"),
                                                              ("failed", "rejected", "failed"),
                                                              ("cancelled", "cancelled", "cancelled"),
                                                              ("blocked", None, "ingested")])
def test_task_finished_settles_ingested_results(rig: Rig, state: str, disposition: str | None, final: str) -> None:
    backend = NodeBackend(rig.studio)
    aid = _ingested(rig, "k1")
    backend.task_finished(rig.task_id, state)
    a = rig.studio.journal.attempts.get(aid)
    assert a is not None and a["state"] == final and a["disposition"] == disposition


def test_backend_identity_and_generation(rig: Rig) -> None:
    backend = NodeBackend(rig.studio)
    assert backend.mode == "nodes" and backend.acquire("gpu1", "aux") == 0 and backend.simulated is False
    assert backend.generation(rig.env, "aux.cutout") == 1
    rig.drive(rig.set_state("failed", **fail("node_unavailable")), rig.call)
    assert backend.generation(rig.env, "aux.cutout") == 1  # generation 2 exists only once the call re-enters
    assert isinstance(rig.drive(rig.set_state("failed", **fail("invalid_input")), rig.call, generation=2),
                      ExecutionFailed)
    assert backend.generation(rig.env, "aux.cutout") == 2
    assert backend.aux() is not None and backend.engine() is not None and backend.worker3d() is not None
    assert backend.worker3d().health()["exporters"] == {"clean": False, "research": False}  # type: ignore[union-attr]


def test_cancel_orphans_cancels_unfinished_attempts(rig: Rig) -> None:
    backend = NodeBackend(rig.studio)
    with pytest.raises(EngineUnavailable):
        rig.studio.settings.runner_offer_ttl_s = 0
        rig.call()
    backend.cancel_orphans([{"id": rig.task_id, "stage": "generate", "progress": {}}])
    a = rig.latest()
    assert a is not None and a["state"] == "cancelled" and a["control"] == "cancel"
