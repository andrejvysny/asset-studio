"""Ephemeral runner profile (WP2.12): one attempt, then the runner deregisters itself. SIMULATED engines: contract
evidence, never GPU proof. The real-hardware node suite is tests/gpu_nodes."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from assetstudio_client import ApiError

from tests.conftest import Api, new_project
from tests.contract import test_node_execution as tne
from tests.contract.test_api_batches import confirm_all, create, detail, setup_project
from tests.contract.test_node_execution import Runner, wait_for
from tests.contract.test_runner_api import Env, Node, reg_token

node_api = tne.node_api  # the in-process nodes-mode app fixture
EPHEMERAL = {"projects": "*", "operations": "*", "labels": [], "ephemeral": True}


def ephemeral_group(api: Api, name: str = "eph") -> str:
    return api.post("/api/v1/runner-groups", {"name": name, **EPHEMERAL})["id"]


class EphemeralRunner(Runner):
    """A simulated runner registered through an ephemeral group; its loop ends once the agent deregisters."""

    def __init__(self, api: Api, tmp: Path, name: str = "eph1") -> None:
        super().__init__(api, tmp, name=name)
        self.group_id = ""

    def start(self) -> EphemeralRunner:
        self.group_id = ephemeral_group(self.api, f"g-{self.cfg.name}")
        self.agent.bootstrap(reg_token(self.api, self.group_id))
        self.agent.open_session()
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.is_set() and not self.agent.stopped:
            try:
                if not self.agent.step():
                    time.sleep(0.02)
            except Exception as e:  # noqa: BLE001 - surfaced by the test
                self.errors.append(e)
                time.sleep(0.1)


def _runner_row(api: Api, runner_id: str) -> dict[str, Any]:
    return next(r for r in api.get("/api/v1/runners")["runners"] if r["id"] == runner_id)


def test_ephemeral_runner_runs_one_attempt_then_deregisters_and_is_never_used_again(
        node_api: Api, tmp_path: Path) -> None:
    api = node_api
    runner = EphemeralRunner(api, tmp_path).start()
    try:
        rid = runner.agent._runner_id  # noqa: SLF001
        assert runner.agent.state.get_identity("ephemeral") == "1"  # RegisterResponse.ephemeral was True
        assert api.get("/api/v1/runner-groups")["groups"][0]["ephemeral"] is True
        pid = setup_project(api)
        bid = create(api, pid, ["Tavern"], "eph-enhance", candidate_count=1)["batch"]["id"]
        # the single attempt (enhance) runs and commits, the receipt is delivered, then the agent deregisters
        wait_for(lambda: _runner_row(api, rid)["state"] == "revoked")
        assert runner.agent.stopped
        (attempt,) = api.studio.journal.attempts.list()
        assert attempt["operation"] == "aux.enhance" and attempt["state"] == "committed"
        assert attempt["disposition"] == "committed" and attempt["runner_id"] == rid
        assert runner.agent.spool.attempt_ids() == [] and runner.agent.state.list_attempts() == []
        assert detail(api, pid, bid)["items"][0]["prompt"]["origin"] == "enhanced"
        events = [a["event"] for a in api.get(f"/api/v1/runners/{rid}")["audit"]]
        assert "register" in events
        # not schedulable any more: no runner counts as ready, so generation is refused at confirm time
        row = _runner_row(api, rid)
        assert row["state"] == "revoked"
        rt = api.get("/api/v1/runtime")
        assert not any(r["ready"] for r in rt["runner_readiness"]), "a revoked runner must not count as ready"
        assert rt["gpus"] == []
        res = confirm_all(api, pid, bid, "eph-confirm")["results"][0]
        assert res["ok"] is False and res["code"] == "missing_models", res  # no runner verifies the image model
        assert [t.stage for t in api.studio.journal.tasks.list()] == ["enhance"]
        assert len(api.studio.journal.attempts.list()) == 1, "no second attempt was offered to the ephemeral runner"
        # Device claims after deregistration: the implementation leaves them `free` (not retired); what keeps the
        # GPU from being scheduled is the revoked runner (no fresh session, not counted by runner_readiness/gpus).
        assert {d["claim"] for d in api.studio.journal.runners.devices(rid)} == {"free"}
        assert [g for g in rt["gpus"] if g["uuid"] in {d["uuid"] for d in _runner_row(api, rid)["devices"]}] == []
    finally:
        runner.stop()
    assert runner.errors == []


def test_ephemeral_runner_refuses_a_second_accept(make_api: Any) -> None:
    api = make_api(coordinator=False)
    env = Env(api, new_project(api))
    node = Node(env, "eph", group_id=ephemeral_group(api))
    node.boot()
    first = node.lease(key="k1")
    node.report(first, "failed")  # the attempt ends, so its device is free for a second offer
    second_task = env.offer(key="k2", task=env.task())  # a second queued call, placed on the only runner
    offer = node.acquire()
    assert offer is not None and offer.attempt_id == second_task["id"]
    with pytest.raises(ApiError) as e:
        node.accept(offer)
    assert e.value.status == 409 and e.value.code == "admission_rejected"


def test_deregister_with_custody_not_transferred_is_refused(make_api: Any) -> None:
    api = make_api(coordinator=False)
    env = Env(api, new_project(api))
    node = Node(env, "eph", group_id=ephemeral_group(api))
    node.boot()
    node.lease()  # non-terminal attempt: the runner still holds the work
    r = node.raw("DELETE", "/runners/self")
    assert r.status_code == 409 and r.json()["code"] == "invalid_input"
    assert _runner_row(api, node.id)["state"] == "active"
    # the agent path covers the delivered-receipt ordering: see the first test (deregister only after spool+state
    # are empty, i.e. after the disposition receipt arrived)
