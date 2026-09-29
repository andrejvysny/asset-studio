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


def test_automatic_retries_are_bounded(make_api, monkeypatch) -> None:
    """H16: no hidden infinite retries of transiently blocked work."""
    api = make_api(coordinator=False)
    j = api.studio.journal
    oid = _op(j, "retry-budget-1")
    for _ in range(runner_mod.MAX_AUTO_ATTEMPTS):
        j.claim_next("gpu0", "inst", None)
        j.finish(oid, "blocked", error={"code": "engine_unavailable", "message": "down", "retryable": True})
        runner_mod.Coordinator(api.studio, {}).retry_blocked()
    op = j.get(oid)
    assert op.state == "blocked" and op.error["retryable"] is False and op.error["retry_budget_exhausted"]


def test_programmer_error_does_not_kill_the_lane(make_api) -> None:
    """RI17: an unexpected exception fails that operation visibly; the coordinator keeps serving the lane."""
    api = make_api(coordinator=False)
    calls: list[str] = []

    def boom(env):  # noqa: ANN001
        raise KeyError("bug")

    def fine(env):  # noqa: ANN001
        calls.append(env.op.id)
        return {"ok": True}
    j = api.studio.journal
    pid = api.post("/api/v1/projects", {"name": "P"})["id"]
    a, _ = j.enqueue(project_id=pid, kind="bad", lane="cpu", affinity="x", payload={}, idempotency_key="bad-op-1")
    b, _ = j.enqueue(project_id=pid, kind="good", lane="cpu", affinity="x", payload={}, idempotency_key="good-op-1")
    c = runner_mod.Coordinator(api.studio, {"bad": (boom, lambda *x: None), "good": (fine, lambda *x: None)})
    for _ in range(2):
        op = c._claim("cpu")
        c.run_one(op)
    assert j.get(a.id).state == "failed" and j.get(a.id).error["code"] == "internal_error"
    assert j.get(b.id).state == "succeeded" and calls == [b.id]
