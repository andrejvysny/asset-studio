"""A build attempt is only ever accepted/published under the candidate approval it was built from.
SIMULATED engines: contract evidence for the binding rules, not model quality."""
from __future__ import annotations

from typing import Any

from assetstudio_server.adapters.fake import FakeAux, FakeEngine, FakeWorker3d
from assetstudio_server.studio import build_studio

from tests.conftest import Api, make_settings
from tests.contract.test_jobs_batches import V2, _batch, _confirm_wave, _items, _job, _run, _setup, _start


def _item(api: Api, pid: str, jid: str) -> dict:
    return api.get(f"{V2}/{pid}/jobs/{jid}")["items"][0]


def _approve_body(item: dict, jid: str, idx: int, key: str) -> dict[str, Any]:
    c = item["candidate_set"]["candidates"][idx]
    return {"idempotency_key": key, "items": [{
        "job_id": jid, "item_id": item["id"], "expected_item_revision": item["revision"],
        "candidate_set_id": item["candidate_set"]["id"], "candidate_id": c["id"], "image_sha256": c["sha256"],
        "prompt_revision_id": item["candidate_set"]["prompt_revision_id"],
        "qa_evaluation_id": c["qa"]["id"] if c["qa"] else None, "override_qa": True}]}


def _accept_body(item: dict, jid: str, run_id: str, key: str, accept: bool = True) -> dict[str, Any]:
    return {"idempotency_key": key, "items": [{"job_id": jid, "item_id": item["id"], "build_run_id": run_id,
                                               "expected_item_revision": item["revision"], "accept": accept}]}


def _walk(api: Api, pid: str, jid: str, base: str) -> None:
    """approve A -> build A -> approve B -> A's attempt is unusable -> approve A again restores it."""
    it = _item(api, pid, jid)
    cand_a, cand_b = (c["id"] for c in it["candidate_set"]["candidates"][:2])
    assert api.post(f"{base}:approve-candidates", _approve_body(it, jid, 0, "bind-approve-a"))["results"][0]["ok"]
    it = _item(api, pid, jid)
    approval_a = it["approval"]
    api.post(f"{base}:build-approved", {"idempotency_key": "bind-build-a", "items": [
        {"job_id": jid, "item_id": it["id"], "approval_id": approval_a, "expected_item_revision": it["revision"]}]})
    api.wait_ops()
    it = _item(api, pid, jid)
    build_a = it["current_build"]
    assert it["build"]["result"] == "valid" and it["legal"]["accept"] and not it["legal"]["build"]
    assert api.post(f"{base}:approve-candidates", _approve_body(it, jid, 1, "bind-approve-b"))["results"][0]["ok"]
    it = _item(api, pid, jid)
    assert it["current_build"] is None and it["approval"] != approval_a and build_a in it["build_runs"]
    bound = it["approval_detail"]["bound"]
    assert bound["restored_build_run_id"] is None and bound["candidate_id"] == cand_b
    row = next(r for r in it["build_history"] if r["id"] == build_a)
    assert row["approval_id"] == approval_a and row["matches_approval"] is False
    assert row["source"] == {"candidate_set_id": it["candidate_set"]["id"], "candidate_id": cand_a, "round": 1}
    refused = api.post(f"{base}:accept-builds", _accept_body(it, jid, build_a, "bind-accept-1"))["results"][0]
    assert refused["ok"] is False and refused["code"] == "approval_mismatch"
    assert api.post(f"{base}:approve-candidates", _approve_body(it, jid, 0, "bind-approve-a2"))["results"][0]["ok"]
    it = _item(api, pid, jid)
    assert it["approval"] not in (approval_a, None) and it["current_build"] == build_a
    assert it["approval_detail"]["bound"]["restored_build_run_id"] == build_a
    assert next(r for r in it["build_history"] if r["id"] == build_a)["matches_approval"] is True
    assert it["legal"]["accept"] and len(it["decisions"]) == 3 and approval_a in it["decisions"]
    ok = api.post(f"{base}:accept-builds", _accept_body(it, jid, build_a, "bind-accept-2"))["results"][0]
    assert ok["ok"], ok
    assert _item(api, pid, jid)["accepted_build"] == build_a


def _mismatch_via_legacy_pointer(api: Api, pid: str, jid: str, base: str) -> None:
    """A legacy item whose current_build still points at an old candidate's attempt cannot be accepted."""
    from assetstudio_server.services.records import mutate_item

    it = _item(api, pid, jid)
    ctx = api.studio.registry.get(pid)
    build_a = it["build_runs"][0]
    assert api.post(f"{base}:approve-candidates", _approve_body(it, jid, 1, "bind-legacy-b"))["results"][0]["ok"]
    mutate_item(api.studio, ctx, jid, it["id"], lambda x: setattr(x, "current_build", build_a))
    it = _item(api, pid, jid)
    assert not it["legal"]["accept"] and it["legal"]["build"]
    r = api.post(f"{base}:accept-builds", _accept_body(it, jid, build_a, "bind-legacy-acc"))["results"][0]
    assert r["ok"] is False and r["code"] == "approval_mismatch"


def _ready_job(api: Api, pid: str, key: str, names: list[str]) -> str:
    jid = _job(api, pid, "props", names, f"job-{key}", candidate_count=2)
    api.post(f"{V2}/{pid}/jobs/{jid}:run", {"idempotency_key": f"run-{key}"})
    api.wait_ops()
    items = api.get(f"{V2}/{pid}/jobs/{jid}")["items"]
    api.post(f"{V2}/{pid}/jobs/{jid}:confirm-and-generate", {"idempotency_key": f"confirm-{key}", "items": [
        {"item_id": i["id"], "prompt_revision_id": i["current_prompt"], "expected_item_revision": i["revision"]}
        for i in items]})
    api.wait_ops()
    return jid


def _api(make_api, tmp_path) -> Api:
    return make_api(studio=build_studio(make_settings(tmp_path), FakeEngine(), FakeAux(), FakeWorker3d()))


def test_job_route_attempt_bound_to_its_candidate(make_api, tmp_path) -> None:
    api = _api(make_api, tmp_path)
    pid = _setup(api)
    jid = _ready_job(api, pid, "bind-job", ["Crate"])
    _walk(api, pid, jid, f"{V2}/{pid}/jobs/{jid}")


def test_legacy_current_build_pointer_cannot_be_accepted(make_api, tmp_path) -> None:
    api = _api(make_api, tmp_path)
    pid = _setup(api)
    jid = _ready_job(api, pid, "bind-legacy", ["Crate"])
    it = _item(api, pid, jid)
    base = f"{V2}/{pid}/jobs/{jid}"
    api.post(f"{base}:approve-candidates", _approve_body(it, jid, 0, "bind-l-a"))
    it = _item(api, pid, jid)
    api.post(f"{base}:build-approved", {"idempotency_key": "bind-l-build", "items": [
        {"job_id": jid, "item_id": it["id"], "approval_id": it["approval"], "expected_item_revision": it["revision"]}]})
    api.wait_ops()
    _mismatch_via_legacy_pointer(api, pid, jid, base)


def test_batch_run_wave_attempt_bound_to_its_candidate(make_api, tmp_path) -> None:
    api = _api(make_api, tmp_path)
    pid = _setup(api)
    jid = _job(api, pid, "props", ["Crate"], "job-bind-wave", candidate_count=2)
    rid = _start(api, pid, _batch(api, pid, [jid]))["run_id"]
    api.wait_ops()
    _confirm_wave(api, pid, rid, _items(_run(api, pid, rid)), "bind-wave-confirm")
    api.wait_ops()
    _walk(api, pid, jid, f"{V2}/{pid}/runs/{rid}")
    assert len(_run(api, pid, rid)["waves"]) >= 4  # 3 approvals + 1 acceptance, each an immutable wave record


def test_unavailable_exporter_override_refused_before_any_task(make_api, tmp_path) -> None:
    api = _api(make_api, tmp_path)
    pid = _setup(api)
    jid = _ready_job(api, pid, "bind-exp", ["Crate"])
    base = f"{V2}/{pid}/jobs/{jid}"
    api.post(f"{base}:approve-candidates", _approve_body(_item(api, pid, jid), jid, 0, "bind-exp-a"))
    it = _item(api, pid, jid)
    api.post(f"{base}:build-approved", {"idempotency_key": "bind-exp-build", "items": [
        {"job_id": jid, "item_id": it["id"], "approval_id": it["approval"], "expected_item_revision": it["revision"]}]})
    api.wait_ops()
    it = _item(api, pid, jid)
    before = len(api.studio.journal.tasks.list())
    r = api.raw("POST", f"{base}:reexport", json={"idempotency_key": "bind-exp-re", "items": [
        {"job_id": jid, "item_id": it["id"], "build_run_id": it["current_build"],
         "expected_item_revision": it["revision"], "overrides": {"exporter": "research"}}]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "exporter_unavailable", r.text
    assert len(api.studio.journal.tasks.list()) == before
