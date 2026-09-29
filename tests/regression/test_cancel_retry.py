"""H04/H16 — RI01, RI02, RI17 on the durable journal (SIMULATED engines)."""
from __future__ import annotations

from pathlib import Path

from assetstudio_server.coordinator import runner as runner_mod
from assetstudio_server.journal import Journal


def _journal(tmp_path: Path) -> Journal:
    return Journal(tmp_path / "j.sqlite")


def _op(j: Journal, key: str = "k-1") -> str:
    op, _ = j.enqueue(project_id="prj_0000000000000000", kind="generate", lane="gpu0", affinity="a",
                      payload={"x": 1}, idempotency_key=key)
    return op.id


def test_cancel_intent_survives_restart(tmp_path: Path) -> None:
    """RI01: cancel a running op, 'restart', reconcile: it finishes cancelled and is never requeued."""
    j = _journal(tmp_path)
    oid = _op(j)
    j.claim_next("gpu0", "inst", None)
    j.request_cancel(oid)
    j.close()
    j2 = _journal(tmp_path)
    assert j2.mark_running_as_reconciling() == []
    assert j2.get(oid).state == "cancelled"  # type: ignore[union-attr]
    assert j2.requeue(oid) is False and j2.get(oid).state == "cancelled"  # type: ignore[union-attr]


def test_cancel_between_blocked_selection_and_requeue(tmp_path: Path) -> None:
    """RI02: a retry that selected a blocked op cannot resurrect it after a concurrent cancel."""
    j = _journal(tmp_path)
    oid = _op(j)
    j.claim_next("gpu0", "inst", None)
    j.finish(oid, "blocked", error={"code": "engine_unavailable", "retryable": True})
    selected = j.list(states=("blocked",))  # the retry loop picked it ...
    j.request_cancel(oid)  # ... the operator cancels ...
    assert j.requeue(selected[0].id, ("blocked",)) is False  # ... and the conditional requeue refuses
    assert j.get(oid).state == "cancelled"  # type: ignore[union-attr]


def test_cancel_requested_then_blocked_resolves_to_cancelled(tmp_path: Path) -> None:
    j = _journal(tmp_path)
    oid = _op(j)
    j.claim_next("gpu0", "inst", None)
    j.request_cancel(oid)
    j.finish(oid, "blocked", error={"code": "engine_unavailable", "retryable": True})
    assert j.get(oid).state == "cancelled"  # type: ignore[union-attr]


def _task(j: Journal, stage: str, key: str, lane: str = "cpu") -> str:
    from assetstudio_server.taskstore import NewTask

    t = NewTask(project_id="prj_0000000000000000", job_id="job_0000000000000000", item_id=f"itm_{key:0>16}"[:20],
                stage=stage, family="build", input_key=key, inputs={}, lane=lane, residency="cpu")
    return j.tasks.create([t], "cmd_test")[0]


def test_automatic_retries_are_bounded(make_api) -> None:
    """H16: no hidden infinite retries of transiently blocked work."""
    api = make_api(coordinator=False)
    j = api.studio.journal
    tid = _task(j, "derive", "retrybudget")
    coord = runner_mod.Coordinator(api.studio)
    for _ in range(runner_mod.MAX_AUTO_ATTEMPTS):
        assert j.tasks.claim(tid, "pas_x")
        j.tasks.finish(tid, "blocked", error={"code": "engine_unavailable", "message": "down", "retryable": True})
        coord.retry_blocked()
    t = j.tasks.get(tid)
    assert t.state == "blocked" and t.error["retryable"] is False and t.error["retry_budget_exhausted"]


def test_task_cancel_intent_survives_restart_and_retry(make_api, tmp_path) -> None:
    """RI01/RI02 for stage tasks: cancel intent is durable; a retry never resurrects cancelled work."""
    api = make_api(coordinator=False)
    j = api.studio.journal
    tid = _task(j, "derive", "cancelrestart")
    assert j.tasks.claim(tid, "pas_x")
    j.tasks.request_cancel([tid])
    assert j.tasks.get(tid).state == "running" and j.tasks.get(tid).control == "cancel_requested"
    assert j.tasks.recover_after_restart()["cancelled"] == 1
    assert j.tasks.get(tid).state == "cancelled" and j.tasks.retry(tid) is False


def test_programmer_error_does_not_kill_the_lane(make_api) -> None:
    """RI17: an unexpected exception fails that task visibly; the coordinator keeps serving the lane."""
    from assetstudio_server.coordinator.stages import Stage

    api = make_api(coordinator=False)
    pid = api.post("/api/v1/projects", {"name": "P"})["id"]
    calls: list[str] = []

    def boom(env):  # noqa: ANN001
        raise KeyError("bug")

    def fine(env):  # noqa: ANN001
        calls.append(env.task.id)
        return {"ok": True}
    j = api.studio.journal
    from assetstudio_server.taskstore import NewTask

    bad, good = (NewTask(project_id=pid, job_id="job_0000000000000000", item_id=f"itm_000000000000000{n}",
                         stage=s, family="build", input_key=s, inputs={}, lane="cpu", residency="cpu")
                 for n, s in ((1, "bad"), (2, "good")))
    j.tasks.create([bad, good], "cmd_test")
    c = runner_mod.Coordinator(api.studio, {"bad": Stage("bad", "build", "cpu", None, boom),
                                            "good": Stage("good", "build", "cpu", None, fine)})
    picked = c.choose("cpu")
    assert picked is not None
    c.run_pass("cpu", *picked)
    assert j.tasks.get(bad.id).state == "failed" and j.tasks.get(bad.id).error["code"] == "internal_error"
    assert j.tasks.get(good.id).state == "succeeded" and calls == [good.id]
