"""R03/R04/R10: one active owner per item stage, deferred downstream re-admission, bounded GPU acquisition."""
from __future__ import annotations

from typing import Any

import pytest
from assetstudio_server.coordinator import reconcile
from assetstudio_server.coordinator.runner import MAX_ADMISSION_FAILURES, Coordinator
from assetstudio_server.coordinator.stages import Stage
from assetstudio_server.taskstore import Busy, NewTask, RunNotOpen, TaskStore

from tests.conftest import Api, new_project
from tests.contract.test_variant_scheduling import pump

JOB, ITEM = "job_0000000000000000", "itm_0000000000000001"


def _t(pid: str, key: str, *, stage: str = "gen", family: str = "generate", lane: str = "cpu",
       run_id: str | None = None, residency: str = "cpu") -> NewTask:
    return NewTask(project_id=pid, job_id=JOB, item_id=ITEM, stage=stage, family=family, input_key=key,
                   inputs={}, lane=lane, residency=residency, run_id=run_id)


def _store(make_api) -> tuple[Api, TaskStore, str]:
    api = make_api(coordinator=False)
    return api, api.studio.journal.tasks, new_project(api)


def test_retry_refuses_second_owner_and_claim_guard(make_api) -> None:
    api, tasks, pid = _store(make_api)
    a = tasks.create([_t(pid, "a")], "cmd_1")[0]
    assert tasks.claim(a, "pas_1")
    tasks.finish(a, "failed", error={"code": "x", "message": "boom"})
    b = tasks.create([_t(pid, "b")], "cmd_2")[0]
    with pytest.raises(Busy) as e:
        tasks.retry(a)
    assert e.value.owner == b
    r = api.raw("POST", f"/api/v2/tasks/{a}:retry")
    assert r.status_code == 409 and r.json()["error"]["code"] == "busy" and b in r.json()["error"]["message"]
    assert tasks.get(a).state == "failed"
    assert tasks.claim(b, "pas_2")
    # forced second queued owner (e.g. legacy data): claim must still refuse while another command runs
    tasks._db.execute("UPDATE stage_tasks SET state='queued' WHERE id=?", (a,))
    assert tasks.claim(a, "pas_3") is False
    assert [t.id for t in tasks.ready("cpu")] == [a]  # ready lists it, claim is the hard guard


def test_retry_same_chain_and_closed_or_paused_run(make_api) -> None:
    api, tasks, pid = _store(make_api)
    x, y = tasks.create([_t(pid, "x", run_id="brn_1"), _t(pid, "y", stage="gen2", run_id="brn_1")], "cmd_1")
    tasks.claim(x, "p")
    tasks.finish(x, "failed", error={"code": "x", "message": "m"})
    tasks.set_run_control(pid, "brn_1", "paused")
    assert tasks.retry(x) is True and tasks.get(x).control == "paused" and tasks.get(x).state == "queued"
    assert x not in [t.id for t in tasks.ready("cpu")] and tasks.claim(x, "p") is False
    tasks._db.execute("UPDATE stage_tasks SET state='failed', control='run' WHERE id=?", (x,))
    tasks.set_run_control(pid, "brn_1", "cancelled")
    with pytest.raises(RunNotOpen):
        tasks.retry(x)
    r = api.raw("POST", f"/api/v2/tasks/{x}:retry")
    assert r.status_code == 409 and r.json()["error"]["code"] == "run_not_open"
    with pytest.raises(RunNotOpen):
        tasks.create([_t(pid, "z", run_id="brn_1")], "cmd_3")


def test_retry_blocked_loop_survives_busy(make_api) -> None:
    api, tasks, pid = _store(make_api)
    a = tasks.create([_t(pid, "a")], "cmd_1")[0]
    tasks.claim(a, "p")
    tasks.finish(a, "blocked", error={"code": "engine_unavailable", "message": "d", "retryable": True})
    b = tasks.create([_t(pid, "b", family="other")], "cmd_2")[0]
    tasks._db.execute("UPDATE stage_tasks SET family='generate' WHERE id=?", (b,))  # legacy double owner
    other = tasks.create([_t(pid, "c", stage="g3", family="build")], "cmd_3")[0]
    tasks.claim(other, "p")
    tasks.finish(other, "blocked", error={"code": "engine_unavailable", "message": "d", "retryable": True})
    Coordinator(api.studio).retry_blocked()  # a is Busy (b owns the stage): stays blocked, loop continues
    assert tasks.get(a).state == "blocked" and tasks.get(other).state == "queued"


def _new_qa(pid: str) -> list[NewTask]:
    return [_t(pid, "q-a", stage="newqa_a", family="qa"), _t(pid, "q-b", stage="newqa_b", family="qa")]


def test_deferred_downstream_is_atomic_visible_and_admitted_after_old_owner(make_api, monkeypatch) -> None:
    api, tasks, pid = _store(make_api)
    old = tasks.create([_t(pid, "old", stage="oldqa", family="qa")], "cmd_old")[0]
    tasks.set_control([old], "paused", ("run",))
    g = tasks.create([_t(pid, "g")], "cmd_new")[0]
    assert tasks.claim(g, "p")
    chain = [_t(pid, "pre", stage="pre", family="other"), *_new_qa(pid)]  # first row would succeed alone
    assert tasks.complete(g, {"candidate_set_id": "cs"}, chain) == "succeeded"
    done = tasks.get(g)
    assert done.downstream_pending is True and done.result["downstream_deferred"] == {"owner": old}
    assert [t.stage for t in tasks.list(command_id="cmd_new")] == ["gen"]  # no partial chain rows
    assert tasks.pending_downstream(pid, ITEM)[0].id == g and tasks.pending_downstream(pid, "itm_other") == []

    monkeypatch.setattr(reconcile, "downstream_for", lambda studio, ctx, t, result: chain if t.stage == "gen" else [])
    reconcile.retry_deferred(api.studio, pid, ITEM)  # old owner still active: stays deferred
    assert tasks.get(g).downstream_pending and len(tasks.list(command_id="cmd_new")) == 1

    stages = {n: Stage(n, "qa", "cpu", None, lambda env: {"ok": True})
              for n in ("oldqa", "pre", "newqa_a", "newqa_b")}
    tasks.set_control([old], "run", ("paused",))
    coord = pump(api, lanes=("cpu",), coord=Coordinator(api.studio, stages))  # old finishes -> chain admitted
    new = [t for t in tasks.list(command_id="cmd_new") if t.stage != "gen"]
    assert sorted(t.stage for t in new) == ["newqa_a", "newqa_b", "pre"]
    assert not tasks.get(g).downstream_pending and all(t.state == "succeeded" for t in new)
    reconcile.retry_deferred(api.studio)
    assert len(tasks.list(command_id="cmd_new")) == 4 and coord is not None  # created exactly once


def test_run_closed_meanwhile_skips_downstream(make_api) -> None:
    api, tasks, pid = _store(make_api)
    g = tasks.create([_t(pid, "g", run_id="brn_2")], "cmd_1")[0]
    tasks.claim(g, "p")
    tasks.set_run_control(pid, "brn_2", "cancelled")
    tasks.complete(g, {"r": 1}, [_t(pid, "q", stage="q", family="qa", run_id="brn_2")])
    t = tasks.get(g)
    assert t.state == "succeeded" and t.result["downstream_skipped"] == "run cancelled" and not t.downstream_pending


class _DownLane:
    sessions: dict[str, Any] = {}

    def __init__(self) -> None:
        self.calls = 0

    def acquire(self, worker: str) -> int:
        self.calls += 1
        raise RuntimeError("GPU1 is held by another process " + "x" * 300)


def test_gpu_acquire_failures_are_visible_then_bounded(make_api) -> None:
    api, tasks, pid = _store(make_api)
    lane = _DownLane()
    api.studio.lanes["gpu1"] = lane  # type: ignore[assignment]
    ids = tasks.create([_t(pid, "a", stage="aux_st", lane="gpu1", residency="r1"),
                        NewTask(project_id=pid, job_id=JOB, item_id="itm_0000000000000002", stage="aux_st",
                                family="generate", input_key="b", inputs={}, lane="gpu1", residency="r1")],
                       "cmd_1")
    coord = Coordinator(api.studio, {"aux_st": Stage("aux_st", "qa", "gpu1", "aux", lambda env: {})})
    for n in range(1, MAX_ADMISSION_FAILURES + 1):
        picked = coord.choose("gpu1")
        assert picked is not None
        coord.run_pass("gpu1", *picked)
        if n < MAX_ADMISSION_FAILURES:
            adm = tasks.get(ids[0]).progress["admission"]
            assert adm["code"] == "resource_unavailable" and adm["failures"] == n and len(adm["message"]) <= 200
            assert coord.status()["admission_failures"]["r1"]["count"] == n
            assert coord.choose("gpu1") is None  # the 20 s backoff still holds
            coord._blocked_until.clear()
    assert lane.calls == MAX_ADMISSION_FAILURES
    for i in ids:
        t = tasks.get(i)
        assert t.state == "blocked" and t.error["code"] == "resource_unavailable" and t.error["retryable"] is False
        assert "retry explicitly" in t.error["message"] and "after 6 attempts" in t.error["message"]
    assert coord.status()["admission_failures"] == {}
    Coordinator(api.studio).retry_blocked()
    assert tasks.get(ids[0]).state == "blocked"  # not retried automatically


def test_deferred_downstream_shows_qa_queued_in_item_progress() -> None:
    from assetstudio_core import lifecycle
    from assetstudio_core.domain import JobItem, TaskRef

    item = JobItem.model_construct(cancelled=False, published=None, accepted_build=None, current_build=None,
                                   approval=None, regen_requested=False, prompt_confirmed="p", current_prompt="p",
                                   current_set="cs", tasks={})
    gen = TaskRef(op_id="stk_1", state="succeeded", progress={"downstream_pending": True})
    stage = lifecycle.item_stage(item, None, {"generate": gen})
    assert stage.state == "QA queued" and lifecycle._progress_label(stage.__dict__, 0, "b") == ("QA queued", "wait")
