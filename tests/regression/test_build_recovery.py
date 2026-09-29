"""H01/H03/H12/H15/H18 — RI07, RI08, RI09, RI12, RI18 with the SIMULATED 3D worker (fault hooks)."""
from __future__ import annotations

from typing import Any

from assetstudio_server.adapters.fake import FakeAux, FakeEngine, FakeWorker3d
from assetstudio_server.coordinator.builds import model3d as m3d
from assetstudio_server.studio import build_studio

from tests.conftest import Api, make_settings
from tests.contract.test_api_batches import approve, confirm_all, create, detail
from tests.contract.test_api_model3d import _budget_project


def _api(make_api, tmp_path) -> tuple[Api, FakeWorker3d]:
    w = FakeWorker3d()
    return make_api(studio=build_studio(make_settings(tmp_path), FakeEngine(), FakeAux(), w)), w


def _approved(api: Api, pid: str, key: str) -> tuple[str, dict]:
    bid = create(api, pid, ["Crate"], f"batch-{key}", category="crates", candidate_count=2)["batch"]["id"]
    api.wait_ops()
    confirm_all(api, pid, bid, f"confirm-{key}")
    api.wait_ops()
    it = detail(api, pid, bid)["items"][0]
    assert approve(api, pid, bid, it, 0, f"approve-{key}")["results"][0]["ok"]
    return bid, detail(api, pid, bid)["items"][0]


def _build(api: Api, pid: str, bid: str, it: dict, key: str) -> dict:
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:build-approved", {"idempotency_key": key, "items": [
        {"item_id": it["id"], "approval_id": it["approval"], "expected_item_revision": it["revision"]}]})
    api.wait_ops()
    return detail(api, pid, bid)["items"][0]


def test_bake_failure_keeps_raw_and_rebuild_does_not_resample(make_api, tmp_path) -> None:
    """RI07 + RI08 (H01): the failed run is in history WITH its raw; rebuilding resumes from it."""
    api, w = _api(make_api, tmp_path)
    pid = _budget_project(api)
    bid, it = _approved(api, pid, "bakefail")
    w.fail_ops["export"] = "oom"
    it = _build(api, pid, bid, it, "build-bakefail-1")
    failed = it["build"]
    assert failed["status"] == "failed" and "raw" in failed["artifacts"] and failed["id"] in it["build_runs"]
    assert failed["validation"]["failure_code"] == "oom" and "sample" in failed["checkpoints"]
    assert w.calls.count("generate") == 1
    it = _build(api, pid, bid, it, "build-bakefail-2")
    ok = it["build"]
    assert ok["result"] == "valid" and ok["id"] != failed["id"]
    assert ok["artifacts"]["raw"] == failed["artifacts"]["raw"] and ok["inputs"]["resumed_from"] == failed["id"]
    assert w.calls.count("generate") == 1  # no second TRELLIS.2 run
    # RI08: a re-export can also start from the failed attempt's raw
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:reexport", {"idempotency_key": "re-from-failed", "items": [
        {"item_id": it["id"], "build_run_id": failed["id"], "expected_item_revision": it["revision"],
         "overrides": {"texture_size": 1024}}]})
    api.wait_ops()
    assert detail(api, pid, bid)["items"][0]["build"]["result"] == "valid"
    assert w.calls.count("generate") == 1


def test_preview_failure_keeps_valid_model(make_api, tmp_path, monkeypatch) -> None:
    """RI09 (H15): a renderer failure never invalidates a structurally valid model."""
    api, _ = _api(make_api, tmp_path)
    pid = _budget_project(api)
    bid, it = _approved(api, pid, "preview")

    def broken(_: bytes) -> bytes:
        raise RuntimeError("renderer crashed")
    monkeypatch.setattr(m3d, "preview_png", broken)
    b = _build(api, pid, bid, it, "build-preview-1")["build"]
    assert b["result"] == "valid" and b["preview"] == "failed" and "renderer" in b["preview_error"]
    assert "model" in b["artifacts"] and "preview" not in b["artifacts"]


def test_spooled_result_recovered_after_studio_restart(make_api, tmp_path) -> None:
    """RI12: a result finished while the Studio was down is fetched by id; nothing is resampled."""
    w = FakeWorker3d()
    engine, aux = FakeEngine(), FakeAux()
    api = make_api(studio=build_studio(make_settings(tmp_path), engine, aux, w))
    pid = _budget_project(api)
    bid, it = _approved(api, pid, "spool")
    w.hold = True
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:build-approved", {"idempotency_key": "build-spool-1", "items": [
        {"item_id": it["id"], "approval_id": it["approval"], "expected_item_revision": it["revision"]}]})
    blocked = _wait_state(api, "blocked")
    run = detail(api, pid, bid)["items"][0]["build"]
    assert run["executions"]["sample"] and run["status"] == "blocked"
    api.c.__exit__(None, None, None)  # Studio stops; the worker finishes and spools the result
    w.hold = False
    w.release_held()
    api2 = make_api(studio=build_studio(make_settings(tmp_path), engine, aux, w))
    api2.post(f"/api/v1/operations/{blocked}:retry")
    api2.wait_ops()
    b = detail(api2, pid, bid)["items"][0]["build"]
    assert b["result"] == "valid" and w.calls.count("generate") == 1
    assert not w.executions  # acknowledged after durable ingestion


def test_lost_execution_is_labelled_not_silently_rerun(make_api, tmp_path) -> None:
    api, w = _api(make_api, tmp_path)
    pid = _budget_project(api)
    bid, it = _approved(api, pid, "lost")
    w.hold = True
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:build-approved", {"idempotency_key": "build-lost-1", "items": [
        {"item_id": it["id"], "approval_id": it["approval"], "expected_item_revision": it["revision"]}]})
    op = _wait_state(api, "blocked")
    w.restart()  # the running TRELLIS.2 execution dies with the worker process
    w.hold = False
    api.post(f"/api/v1/operations/{op}:retry")
    api.wait_ops()
    b = detail(api, pid, bid)["items"][0]["build"]
    assert b["status"] == "failed" and b["validation"]["failure_code"] == "lost_execution"
    assert w.calls.count("generate") == 1  # the explicit next step is a new build, not an automatic resubmit


def test_reexport_target_beats_category_max(make_api, tmp_path) -> None:
    """RI18 (H12): an explicit re-export target overrides the category-derived default; min/max stay advisory."""
    api, w = _api(make_api, tmp_path)
    pid = _budget_project(api)  # crates: triangles min 100 max 5000
    bid, it = _approved(api, pid, "target")
    first = _build(api, pid, bid, it, "build-target-1")["build"]
    it = detail(api, pid, bid)["items"][0]
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:reexport", {"idempotency_key": "re-target-1", "items": [
        {"item_id": it["id"], "build_run_id": first["id"], "expected_item_revision": it["revision"],
         "overrides": {"triangles": 20000}}]})
    api.wait_ops()
    b = detail(api, pid, bid)["items"][0]["build"]
    budget = _meta(api, pid, b)["budget"]
    assert budget["requested"] == 20000 and budget["effective"] == 20000 and budget["source"] == "re-export override"
    assert budget["max"] == 5000 and budget["advisory"] is True


def test_missing_exporter_rejected_before_sampling(make_api, tmp_path, monkeypatch) -> None:
    """H18: an unavailable exporter is found before TRELLIS.2 runs, not after."""
    api, w = _api(make_api, tmp_path)
    pid = _budget_project(api)
    bid, it = _approved(api, pid, "exporter")
    monkeypatch.setattr(w, "health", lambda: {"reachable": True, "ok": True, "exporters": {"clean": False}})
    out = api.post(f"/api/v1/projects/{pid}/batches/{bid}:build-approved", {"idempotency_key": "build-exp-1", "items": [
        {"item_id": it["id"], "approval_id": it["approval"], "expected_item_revision": it["revision"]}]})
    assert out["results"][0]["code"] == "exporter_unavailable" and out["operation"] is None
    assert w.calls.count("generate") == 0


def _wait_state(api: Api, state: str) -> str:
    import time

    end = time.monotonic() + 10
    while time.monotonic() < end:
        tasks = api.studio.journal.tasks.list(states=(state,))
        if tasks:
            return tasks[0].id
        time.sleep(0.05)
    raise AssertionError(f"no {state} task")


def _meta(api: Api, pid: str, build: dict) -> Any:
    import json

    return json.loads(api.raw("GET", f"/api/v1/projects/{pid}/artifacts/{build['artifacts']['meta']}/content").content)
