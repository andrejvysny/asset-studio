"""Node execution (STUDIO_EXECUTION=nodes): the unchanged stage code runs its engine calls on an in-process runner
(real agent + client over the real app, SIMULATED engines). Contract evidence, never GPU proof."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import assetstudio_node.agent as node_agent
import pytest
from assetstudio_client import RunnerClient
from assetstudio_node.agent import RunnerAgent, load_or_create_key
from assetstudio_node.config import RunnerConfig
from assetstudio_node.engine_executor import EngineExecutor, Engines, build_engines
from assetstudio_node.engines.fake import FakeEngine
from assetstudio_node.spool import Spool
from assetstudio_node.state import RunnerState
from assetstudio_protocol.execution import Requirements
from assetstudio_server.coordinator.stages.base import model_identity
from assetstudio_server.main import create_app
from assetstudio_server.services.placement import eligible_slots
from assetstudio_server.studio import build_studio
from fastapi.testclient import TestClient

from tests.conftest import ROOT, Api, make_settings
from tests.contract.test_api_batches import approve, confirm_all, create, detail, setup_project
from tests.contract.test_api_model3d import _budget_project, _built, _meta
from tests.contract.test_runner_api import BASE, make_group, reg_token

log = logging.getLogger("assetstudio.test")
FAST = 400.0  # runner clock speed-up: heartbeats (cancel, receipts) arrive within milliseconds


class Runner:
    """A simulated runner (image slot + aux/3D slot) driven by a background thread against the app."""

    def __init__(self, api: Api, tmp: Path, name: str = "r1", image_steps: int = 1) -> None:
        self.api, self.errors = api, []
        self.cfg = RunnerConfig.model_validate({
            "studio_url": BASE, "name": name, "state_dir": str(tmp / f"state-{name}"),
            "host_lock": str(tmp / f"lock-{name}"), "dispatch": "pull", "acquire_wait_s": 0, "simulated": True,
            "poll_s": 0.01, "slots": [
                {"slot_id": "gpu-img", "capability": "image", "devices": [f"GPU-img-{name}"], "engines": ["comfyui"]},
                {"slot_id": "gpu-aux", "capability": "aux3d", "devices": [f"GPU-aux-{name}"], "engines": ["aux", "worker3d"]}]})
        engines = build_engines(self.cfg)
        engines.comfy = FakeEngine(steps_to_finish=image_steps)
        self.engines: Engines = engines
        state = RunnerState(self.cfg.state_dir)
        client = RunnerClient(BASE, private_key=load_or_create_key(self.cfg), http=TestClient(api.c.app))
        self.agent = RunnerAgent(self.cfg, client=client, executor=EngineExecutor(self.cfg, state, engines),
                                 state=state, spool=Spool(self.cfg.state_dir / "spool"),
                                 clock=lambda: time.monotonic() * FAST, idle_s=0.0, concurrent=False)
        self._stop, self._thread = threading.Event(), threading.Thread(target=self._loop, daemon=True)

    def start(self) -> Runner:
        self.agent.bootstrap(reg_token(self.api, make_group(self.api, f"g-{self.cfg.name}")))
        self.agent.open_session()
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.agent.step():
                    time.sleep(0.02)
            except Exception as e:  # noqa: BLE001 - surfaced by the test, the loop keeps serving
                self.errors.append(e)
                log.exception("runner step failed")
                time.sleep(0.1)

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(5)


@pytest.fixture
def node_api(tmp_path: Path) -> Iterator[Api]:
    settings = make_settings(tmp_path)
    settings.execution = "nodes"
    settings.runner_offer_ttl_s = 5
    studio = build_studio(settings)
    client = TestClient(create_app(settings, studio))
    client.__enter__()
    yield Api(client, studio)
    client.__exit__(None, None, None)


@pytest.fixture
def runner_factory(node_api: Api, tmp_path: Path) -> Iterator[Callable[..., Runner]]:
    started: list[Runner] = []

    def make(**kw: Any) -> Runner:
        r = Runner(node_api, tmp_path, **kw).start()
        started.append(r)
        return r
    yield make
    for r in started:
        r.stop()
        assert r.errors == []


def wait_for(pred: Callable[[], bool], timeout: float = 20.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end and not pred():
        time.sleep(0.05)
    assert pred()


def all_attempts(api: Api) -> list[dict[str, Any]]:
    return api.studio.journal.attempts.list()


def settle(api: Api, runner: Runner) -> list[dict[str, Any]]:
    """Every attempt committed and its receipt delivered: the runner's spool and state are empty."""
    wait_for(lambda: bool(all_attempts(api)) and all(a["disposition"] is not None for a in all_attempts(api)))
    wait_for(lambda: runner.agent.spool.attempt_ids() == [] and runner.agent.state.list_attempts() == [])
    return all_attempts(api)


def test_nodes_mode_has_no_local_engines(node_api: Api) -> None:
    s = node_api.studio
    assert s.execution.mode == "nodes" and (s.engine, s.aux, s.worker3d) == (None, None, None)
    assert s.lanes["gpu1"].sessions == {} and s.simulated is False


def test_concept_art_lifecycle_runs_every_engine_call_on_the_runner(node_api: Api, runner_factory: Any) -> None:
    api, runner = node_api, runner_factory()
    pid = setup_project(api)
    bid = create(api, pid, ["Tavern interior"], "node-single-1")["batch"]["id"]
    api.wait_ops()
    item = detail(api, pid, bid)["items"][0]
    assert item["prompt"]["origin"] == "enhanced" and item["candidate_set"] is None
    confirm_all(api, pid, bid, "node-confirm-1")
    api.wait_ops()
    item = detail(api, pid, bid)["items"][0]
    cands = item["candidate_set"]["candidates"]
    assert len(cands) == 4 and all(c["qa"] is not None for c in cands) and len({c["seed"] for c in cands}) == 4
    assert approve(api, pid, bid, item, 1, "node-approve-1")["results"][0]["ok"]
    item = detail(api, pid, bid)["items"][0]
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:build-approved", {"idempotency_key": "node-build-1", "items": [
        {"item_id": item["id"], "approval_id": item["approval"], "expected_item_revision": item["revision"]}]})
    api.wait_ops()
    item = detail(api, pid, bid)["items"][0]
    assert item["build"]["result"] == "valid"
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:accept-builds", {"idempotency_key": "node-accept-1", "items": [
        {"item_id": item["id"], "build_run_id": item["current_build"], "expected_item_revision": item["revision"]}]})
    prev = api.get(f"/api/v1/projects/{pid}/batches/{bid}/publish-preview")["items"]
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:publish", {"idempotency_key": "node-publish-1", "items": [
        {"item_id": p["item_id"], "build_run_id": p["build_run_id"], "expected_item_revision": p[
            "expected_item_revision"]} for p in prev]})
    api.wait_ops()
    lib = api.get(f"/api/v1/projects/{pid}/assets")
    assert lib["total"] == 1 and lib["items"][0]["origin"] == "generated"
    asset = api.get(f"/api/v1/projects/{pid}/assets/{lib['items'][0]['asset_id']}")
    assert asset["shown_version"]["sources"]["candidate_id"] == cands[1]["id"]
    assert {f["role"] for f in asset["files"]} == {"image", "preview"}
    rows = settle(api, runner)
    ops = {a["operation"] for a in rows}
    assert {"aux.enhance", "image.t2i", "aux.qa"} <= ops
    assert sum(a["operation"] == "image.t2i" for a in rows) == 4
    assert all(a["state"] == "committed" and a["disposition"] == "committed" for a in rows), \
        [(a["operation"], a["state"], a["disposition"]) for a in rows]
    assert max(a["generation"] for a in rows) == 1  # nothing was re-placed
    assert all(a["runner_id"] for a in rows)
    assert runner.engines.comfy.calls and runner.engines.aux.calls  # the runner's engines did the work


def test_model3d_build_runs_sample_and_bake_on_the_runner(node_api: Api, runner_factory: Any) -> None:
    api, runner = node_api, runner_factory()
    pid = _budget_project(api)
    bid, it = _built(api, pid, "crates", "node3d-1")
    b = it["build"]
    assert b["result"] == "valid", b["validation"]
    assert b["artifacts"].keys() >= {"model", "raw", "cutout", "preview", "meta"}
    checks = {c["id"]: c for c in b["validation"]["checks"]}
    assert checks["material_texture_present"]["ok"] and checks["uvs_present"]["ok"]
    assert _meta(api, pid, b)["mask"]["source"] == "qa_reused"
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:accept-builds", {"idempotency_key": "node3d-acc", "items": [
        {"item_id": it["id"], "build_run_id": it["current_build"], "expected_item_revision": it["revision"]}]})
    prev = api.get(f"/api/v1/projects/{pid}/batches/{bid}/publish-preview")["items"]
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:publish", {"idempotency_key": "node3d-pub", "items": [
        {"item_id": p["item_id"], "build_run_id": p["build_run_id"], "expected_item_revision": p[
            "expected_item_revision"]} for p in prev]})
    api.wait_ops()
    lib = api.get(f"/api/v1/projects/{pid}/assets")
    asset = api.get(f"/api/v1/projects/{pid}/assets/{lib['items'][0]['asset_id']}")
    assert {f["role"] for f in asset["files"]} >= {"model", "preview", "meta"}
    rows = settle(api, runner)
    w3d = sorted(a["operation"] for a in rows if a["operation"].startswith("worker3d."))
    assert w3d == ["worker3d.export", "worker3d.generate"]
    assert all(a["state"] == "committed" for a in rows) and max(a["generation"] for a in rows) == 1
    assert [c for c in runner.engines.worker3d.calls if c != "unload"] == ["generate", "export:clean"]


def test_enhance_blocks_without_a_runner_then_completes_once_one_registers(
        node_api: Api, runner_factory: Any) -> None:
    api = node_api
    pid = setup_project(api)
    assert api.get("/api/v1/projects")  # the library works with no GPUs and no runners
    bid = create(api, pid, ["Tavern"], "node-norunner-1", candidate_count=1)["batch"]["id"]
    wait_for(lambda: any(t.state == "blocked" for t in api.studio.journal.tasks.list()))
    (task,) = api.studio.journal.tasks.list()
    assert task.stage == "enhance" and task.error and "no eligible runner" in task.error["message"]
    assert task.error["retryable"] is True
    assert api.get(f"/api/v1/projects/{pid}/assets")["total"] == 0
    assert detail(api, pid, bid)["items"][0]["candidate_set"] is None
    runner_factory()
    api.c.app.state.coordinator.retry_blocked()
    wait_for(lambda: api.studio.journal.tasks.get(task.id).state == "succeeded")  # type: ignore[union-attr]
    assert detail(api, pid, bid)["items"][0]["prompt"]["origin"] == "enhanced"
    (attempt,) = all_attempts(api)
    assert attempt["operation"] == "aux.enhance" and attempt["generation"] == 1


def test_cancelling_a_task_cancels_its_running_attempt(node_api: Api, runner_factory: Any) -> None:
    api = node_api
    runner = runner_factory(image_steps=600)  # ~6 s of simulated image work at 10 ms polls
    pid = setup_project(api)
    bid = create(api, pid, ["Tavern"], "node-cancel-1")["batch"]["id"]
    api.wait_ops()
    confirm_all(api, pid, bid, "node-cancel-confirm")
    wait_for(lambda: any(a["operation"] == "image.t2i" and a["state"] == "executing" for a in all_attempts(api)))
    (task,) = [t for t in api.studio.journal.tasks.list() if t.stage == "generate"]
    api.post(f"/api/v2/tasks/{task.id}:cancel")
    wait_for(lambda: api.studio.journal.tasks.get(task.id).state == "cancelled")  # type: ignore[union-attr]
    a = next(x for x in all_attempts(api) if x["operation"] == "image.t2i")
    assert a["control"] == "cancel"
    wait_for(lambda: api.studio.journal.attempts.get(a["id"])["state"] == "cancelled")  # type: ignore[index]
    assert ("cancel", next(c[1] for c in runner.engines.comfy.calls if c[0] == "submit")) in runner.engines.comfy.calls


def _recipe(api: Api, recipe_id: str) -> dict[str, Any]:
    return next(r for r in api.get("/api/v1/runtime")["recipes"] if r["id"] == recipe_id)


def _receipts_as(monkeypatch: pytest.MonkeyPatch, status_by_key: dict[str, dict[str, str]]) -> None:
    """Runner `name` reports the given receipt statuses (key -> status; "absent" drops the receipt)."""
    real = node_agent.session_receipts

    def patched(config: RunnerConfig, *a: Any) -> Any:
        wanted = status_by_key.get(config.name, {})
        return [r.model_copy(update={"status": wanted.get(r.key, r.status)}) for r in real(config, *a)
                if wanted.get(r.key) != "absent"]
    monkeypatch.setattr(node_agent, "session_receipts", patched)


def test_runtime_without_runners_reports_why_and_the_library_still_works(node_api: Api) -> None:
    api = node_api  # A01: no model weights and no runner on the Studio host
    assert api.get("/api/v1/projects") is not None
    rt = api.get("/api/v1/runtime")
    assert rt["engine_mode"] == "nodes" and rt["simulated"] is False and rt["gpus"] == []
    gen = _recipe(api, "concept.default")["generation"]
    assert gen["state"] != "ready" and gen["state"] != "experimental"
    ready = {r["operation"]: r for r in rt["runner_readiness"]}
    assert len(ready) == 10 and not any(r["ready"] for r in ready.values())
    assert all("runner" in " ".join(r["reasons"]) for r in ready.values())
    assert all(m["status"] == "missing" and "runner" in m["detail"] for m in rt["models"])
    assert [s["ready"] for s in rt["services"]] == [False, False, False]


def test_runner_declared_readiness_drives_runtime_and_recipes(node_api: Api, runner_factory: Any) -> None:
    runner_factory()
    rt = node_api.get("/api/v1/runtime")
    assert rt["simulated"] is True and all(r["ready"] for r in rt["runner_readiness"])
    assert {g["uuid"] for g in rt["gpus"]} == {"GPU-img-r1", "GPU-aux-r1"}
    assert {g["lane"] for g in rt["gpus"]} == {"image", "aux3d"}
    assert all(g["vram_used_mb"] is None and g["util_pct"] is None and g["measured_at"] is None for g in rt["gpus"])
    assert all(m["ready"] and m["full_verified"] for m in rt["models"])
    assert _recipe(node_api, "concept.default")["generation"]["state"] == "experimental"


def test_a_corrupt_receipt_blocks_only_the_recipes_that_need_it(
        node_api: Api, runner_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _receipts_as(monkeypatch, {"r1": {"trellis2": "corrupt"}})  # A05
    runner_factory()
    model3d = _recipe(node_api, "model3d.default")["build"]
    assert model3d["state"] == "missing_models" and "trellis2" in model3d["reason"]
    assert _recipe(node_api, "icon.default")["build"]["state"] == "ready"
    assert _recipe(node_api, "model3d.default")["generation"]["state"] == "experimental"
    trellis = next(m for m in node_api.get("/api/v1/runtime")["models"] if m["key"] == "trellis2")
    assert trellis["status"] == "corrupt" and "runner r1" in trellis["detail"]


def test_a_model_verified_on_one_runner_only_keeps_the_recipe_ready_and_places_there(
        node_api: Api, runner_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _receipts_as(monkeypatch, {"a": {"trellis2": "absent"}})  # A04: only runner b holds the weights
    a, b = runner_factory(name="a"), runner_factory(name="b")
    assert _recipe(node_api, "model3d.default")["build"]["state"] == "ready"
    req = Requirements(capability="aux3d", engine="worker3d", models=[model_identity(ROOT / "config", "trellis2")])
    choices, reasons = eligible_slots(node_api.studio, operation="worker3d.generate", requirements=req,
                                      project_id="prj_x")
    by_id = {r["id"]: r["name"] for r in node_api.studio.auth.runners()}
    assert {by_id[c.runner_id] for c in choices} == {"b"}
    assert any("runner a: model trellis2 not verified (absent)" in r for r in reasons)
    assert a.errors == [] and b.errors == []
