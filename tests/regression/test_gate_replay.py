"""Candidate approval and final acceptance are replay-safe durable commands (plan without writes, durable intent,
idempotent effects). SIMULATED engines."""
from __future__ import annotations

import pytest
from assetstudio_server.services import commands, review

from tests.conftest import Api
from tests.contract.test_jobs_batches import V2, _setup
from tests.contract.test_review_binding import _accept_body, _api, _item, _ready_job


def _two_item_body(api: Api, pid: str, jid: str, key: str) -> dict:
    items = api.get(f"{V2}/{pid}/jobs/{jid}")["items"]
    body = []
    for i in items:
        c = i["candidate_set"]["candidates"][0]
        body.append({"job_id": jid, "item_id": i["id"], "expected_item_revision": i["revision"],
                     "candidate_set_id": i["candidate_set"]["id"], "candidate_id": c["id"],
                     "image_sha256": c["sha256"], "prompt_revision_id": i["candidate_set"]["prompt_revision_id"],
                     "qa_evaluation_id": c["qa"]["id"] if c["qa"] else None, "override_qa": True})
    return {"idempotency_key": key, "items": body}


def _crash_on(monkeypatch: pytest.MonkeyPatch, nth: int) -> None:
    real, calls = review.outcome, {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == nth:
            raise RuntimeError("process died mid-effects")
        return real(*a, **k)
    monkeypatch.setattr(review, "outcome", flaky)


def _post_crash(api: Api, url: str, body: dict) -> None:
    with pytest.raises(RuntimeError):
        api.c.post(url, json=body, headers={"x-assetstudio": "1"})


def test_approve_replays_exactly_once_after_partial_effects(make_api, tmp_path, monkeypatch) -> None:
    api = _api(make_api, tmp_path)
    pid = _setup(api)
    jid = _ready_job(api, pid, "replay-a", ["Crate", "Barrel"])
    body = _two_item_body(api, pid, jid, "replay-approve-1")
    url = f"{V2}/{pid}/jobs/{jid}:approve-candidates"
    with monkeypatch.context() as m:
        _crash_on(m, 2)
        _post_crash(api, url, body)
    items = api.get(f"{V2}/{pid}/jobs/{jid}")["items"]
    assert [bool(i["approval"]) for i in items] == [True, False]  # first unit's write survived the crash
    commands.replay_open_intents(api.studio)
    again = api.post(url, body)
    assert [r["ok"] for r in again["results"]] == [True, True]
    assert [r["item_id"] for r in again["results"]] == [i["id"] for i in items]
    for i in api.get(f"{V2}/{pid}/jobs/{jid}")["items"]:
        assert len(i["decisions"]) == 1 and i["approval"] == i["decisions"][0]
    assert api.post(url, body) == again


def test_replay_does_not_approve_an_item_that_changed_since_planning(make_api, tmp_path, monkeypatch) -> None:
    api = _api(make_api, tmp_path)
    pid = _setup(api)
    jid = _ready_job(api, pid, "replay-b", ["Crate", "Barrel"])
    body = _two_item_body(api, pid, jid, "replay-approve-2")
    url = f"{V2}/{pid}/jobs/{jid}:approve-candidates"
    with monkeypatch.context() as m:
        _crash_on(m, 1)
        _post_crash(api, url, body)
    first = _item(api, pid, jid)
    edit = api.post(f"{V2}/{pid}/jobs/{jid}:edit-prompts", {"items": [
        {"item_id": first["id"], "expected_item_revision": first["revision"], "description": "a different crate"}]})
    assert edit["results"][0]["ok"]
    commands.replay_open_intents(api.studio)
    res = api.post(url, body)["results"]
    assert res[0]["ok"] is False and res[0]["code"] == "stale_item" and res[1]["ok"] is True
    items = api.get(f"{V2}/{pid}/jobs/{jid}")["items"]
    assert items[0]["approval"] is None and items[0]["decisions"] == [] and items[1]["approval"]


def test_accept_replays_and_conflicting_body_is_refused(make_api, tmp_path, monkeypatch) -> None:
    api = _api(make_api, tmp_path)
    pid = _setup(api)
    jid = _ready_job(api, pid, "replay-c", ["Crate"])
    base = f"{V2}/{pid}/jobs/{jid}"
    api.post(f"{base}:approve-candidates", _two_item_body(api, pid, jid, "replay-approve-3"))
    it = _item(api, pid, jid)
    api.post(f"{base}:build-approved", {"idempotency_key": "replay-build-3", "items": [
        {"job_id": jid, "item_id": it["id"], "approval_id": it["approval"], "expected_item_revision": it["revision"]}]})
    api.wait_ops()
    it = _item(api, pid, jid)
    body = _accept_body(it, jid, it["current_build"], "replay-accept-3")
    with monkeypatch.context() as m:
        real = review.outcome

        def die_after_write(*a, **k):
            real(*a, **k)
            raise RuntimeError("process died after the item write")
        m.setattr(review, "outcome", die_after_write)
        _post_crash(api, f"{base}:accept-builds", body)
    assert _item(api, pid, jid)["accepted_build"] == it["current_build"]
    commands.replay_open_intents(api.studio)
    res = api.post(f"{base}:accept-builds", body)["results"][0]
    assert res["ok"] is True
    after = _item(api, pid, jid)
    assert len(after["decisions"]) == 2 and after["accepted_build"] == it["current_build"]
    other = {**body, "items": [{**body["items"][0], "accept": False}]}
    r = api.raw("POST", f"{base}:accept-builds", json=other)
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_conflict"
