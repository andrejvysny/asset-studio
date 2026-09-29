"""Restart / lost-acknowledgement recovery with the SIMULATED engine (R01, R03, R04)."""
from __future__ import annotations

import time

from assetstudio_server.adapters.fake import FakeAux, FakeEngine
from assetstudio_server.studio import build_studio

from tests.conftest import make_settings
from tests.contract.test_api_batches import confirm_all, create, detail, setup_project


def test_queued_work_survives_restart(make_api, tmp_path) -> None:
    api = make_api(coordinator=False)
    pid = setup_project(api)
    bid = create(api, pid, ["A", "B"], "batch-restart-1")["batch"]["id"]
    assert [o.state for o in api.studio.journal.list()] == ["queued"]
    api.c.__exit__(None, None, None)
    api2 = make_api(coordinator=True)
    api2.wait_ops()
    items = detail(api2, pid, bid)["items"]
    assert all(i["prompt"] is not None for i in items)


def test_running_op_is_reconciled_not_duplicated(make_api, tmp_path) -> None:
    engine, aux = FakeEngine(), FakeAux()
    api = make_api(coordinator=False, studio=build_studio(make_settings(tmp_path), engine, aux))
    pid = setup_project(api)
    bid = create(api, pid, ["A"], "batch-recon-1", enhance=False)["batch"]["id"]
    # simulate a crash mid-generation: op claimed ("running"), 2 of 4 prompts already submitted to the engine
    item = detail(api, pid, bid)["items"][0]
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:edit-prompts", {"items": [
        {"item_id": item["id"], "expected_item_revision": item["revision"], "description": "a barrel"}]})
    confirm_all(api, pid, bid, "confirm-recon-1")
    op = api.studio.journal.claim_next("gpu0", "old-instance", None)
    assert op is not None and op.state == "running"
    api.c.__exit__(None, None, None)
    api2 = make_api(coordinator=True, studio=build_studio(make_settings(tmp_path), engine, aux))
    api2.wait_ops()
    op2 = api2.studio.journal.get(op.id)
    assert op2.state == "succeeded" and op2.progress.get("reconciled_after_restart") is True
    assert len(detail(api2, pid, bid)["items"][0]["candidate_set"]["candidates"]) == 4
    assert sum(1 for c in engine.calls if c[0] == "submit") == 4


def test_lost_submit_ack_reconciled_by_prompt_id(make_api, tmp_path) -> None:
    engine, aux = FakeEngine(), FakeAux()
    engine.lose_ack = 1
    api = make_api(studio=build_studio(make_settings(tmp_path), engine, aux))
    pid = setup_project(api)
    bid = create(api, pid, ["A"], "batch-lost-1")["batch"]["id"]
    api.wait_ops()
    confirm_all(api, pid, bid, "confirm-lost-1")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not api.studio.journal.list(states=("blocked",)):
        time.sleep(0.05)
    blocked = api.studio.journal.list(states=("blocked",))
    assert blocked and blocked[0].error["retryable"] is True
    item = detail(api, pid, bid)["items"][0]
    assert item["tasks"]["generate"]["state"] == "blocked"
    api.post(f"/api/v1/operations/{blocked[0].id}:retry")
    api.wait_ops()
    item = detail(api, pid, bid)["items"][0]
    assert len(item["candidate_set"]["candidates"]) == 4
    assert sum(1 for c in engine.calls if c[0] == "submit") == 4  # the lost one was found, not resubmitted


def test_cancel_queued_generation_releases_items(make_api) -> None:
    api = make_api(coordinator=False)
    pid = setup_project(api)
    bid = create(api, pid, ["A"], "batch-cancel-1", enhance=False)["batch"]["id"]
    item = detail(api, pid, bid)["items"][0]
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:edit-prompts", {"items": [
        {"item_id": item["id"], "expected_item_revision": item["revision"], "description": "x"}]})
    out = confirm_all(api, pid, bid, "confirm-cancel-1")
    op_id = out["operations"][0]["id"]
    res = api.post(f"/api/v1/operations/{op_id}:cancel")
    assert res["state"] == "cancelled"
    item = detail(api, pid, bid)["items"][0]
    assert item["tasks"]["generate"]["state"] == "cancelled" and item["legal"]["confirm"] is True
