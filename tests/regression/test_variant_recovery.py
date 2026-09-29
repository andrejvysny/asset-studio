"""Variant recovery acceptance (VR01-VR08): crashes, lost acknowledgements, cancellation and per-item failure isolation
for the variant feature. SIMULATED engines with fault hooks: contract evidence for recovery logic, never GPU proof."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from assetstudio_core.ids import derived_id
from assetstudio_server.adapters.base import engine_prompt_id
from assetstudio_server.adapters.fake import FakeAux, FakeEngine, FakeWorker3d
from assetstudio_server.coordinator import reconcile
from assetstudio_server.coordinator.runner import TaskEnv, wait_until
from assetstudio_server.coordinator.stages.generate import generate
from assetstudio_server.services import jobs as jobs_svc
from assetstudio_server.services import runs as runs_svc
from assetstudio_server.services.records import load_item
from assetstudio_server.services.variant_jobs import load_plan
from assetstudio_server.studio import build_studio
from assetstudio_storage.families import list_families

from tests.conftest import HEADERS, Api, make_settings, new_project
from tests.contract.test_api_variants import _draft, _png_src, _prepare, _rows
from tests.contract.test_jobs_batches import _batch, _confirm_wave, _items, _run, _start
from tests.contract.test_variant_scheduling import pump, studio_api, variant_jobs

P = "/api/v1/projects"
V2 = "/api/v2/projects"


def restart(make_api: Any, old: Api, tmp_path: Path, engine: FakeEngine, aux: FakeAux, w3d: FakeWorker3d,
            **kw: Any) -> Api:
    """Studio process restart on the same instance dir; the simulated engines/worker outlive it."""
    old.c.__exit__(None, None, None)
    return make_api(studio=build_studio(make_settings(tmp_path), engine, aux, w3d), **kw)


def _item(api: Api, pid: str, jid: str) -> dict:
    return api.get(f"{V2}/{pid}/jobs/{jid}")["items"][0]


def _enhance(api: Api, pid: str, jid: str, key: str) -> dict:
    api.post(f"{V2}/{pid}/jobs/{jid}:run", {"idempotency_key": f"run-{key}"})
    api.wait_ops()
    return _item(api, pid, jid)


def _confirm(api: Api, pid: str, jid: str, key: str, item: dict | None = None) -> dict:
    it = item or _item(api, pid, jid)
    return api.post(f"{V2}/{pid}/jobs/{jid}:confirm-and-generate", {"idempotency_key": key, "items": [
        {"item_id": it["id"], "prompt_revision_id": it["current_prompt"], "expected_item_revision": it["revision"]}]})


def _edit_calls(engine: FakeEngine) -> list[str]:
    return [c[1] for c in engine.calls if c[0] == "submit_edit"]


# --- VR01 ------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("crash_at", ["first_job", "second_job", "batch"])
def test_vr01_crash_after_create_jobs_intent_replays_exactly_once(make_api, tmp_path, monkeypatch, crash_at) -> None:
    """The intent commits, the effects die part-way (plan written, some/no Jobs, no Batch); restart finishes them."""
    api, engine, aux, w3d = studio_api(make_api, tmp_path)
    pid = new_project(api)
    src = _png_src(api, pid)
    d = _prepare(api, pid, _draft(api, pid, src, "image_edit", "draft-vr01-1", rows=_rows(3), requested_variants=3))
    real_write, real_group = jobs_svc.write_job, runs_svc.write_batch_group
    written: list[str] = []

    def dying_job(ctx: Any, j: dict) -> Any:
        if crash_at == "first_job" or (crash_at == "second_job" and len(written) == 1):
            raise RuntimeError("process died in the effects")
        written.append(j["job_id"])
        return real_write(ctx, j)

    def dying_group(*a: Any, **k: Any) -> Any:
        raise RuntimeError("process died before the Batch record")
    monkeypatch.setattr(jobs_svc, "write_job", dying_job)
    monkeypatch.setattr(runs_svc, "write_batch_group", dying_group)
    body = {"expected_revision": d["revision"], "idempotency_key": "jobs-vr01-1"}
    url = f"{P}/{pid}/variant-drafts/{d['id']}:create-jobs"
    with pytest.raises(RuntimeError):
        api.c.post(url, json=body, headers=HEADERS)
    assert len(api.studio.journal.tasks.open_intents()) == 1
    assert len(written) == {"first_job": 0, "second_job": 1, "batch": 3}[crash_at]  # partial effects on disk
    monkeypatch.setattr(jobs_svc, "write_job", real_write)
    monkeypatch.setattr(runs_svc, "write_batch_group", real_group)

    api2 = restart(make_api, api, tmp_path, engine, aux, w3d)
    assert api2.studio.journal.tasks.open_intents() == []
    ctx = api2.studio.registry.get(pid)
    jobs = api2.get(f"{V2}/{pid}/jobs")["jobs"]
    batches = api2.get(f"{V2}/{pid}/batches")["batches"]
    assert len(jobs) == 3 and len({j["id"] for j in jobs}) == 3 and len(batches) == 1
    assert set(batches[0]["job_ids"]) == {j["id"] for j in jobs}
    assert len(ctx.store.repo.list_keys("variant-plans/")[0]) == 1
    fams = list_families(ctx.store)
    assert len(fams) == 1 and api2.get(f"{P}/{pid}/assets/{src['asset_id']}")["family_id"] == fams[0].id
    assert api2.studio.journal.tasks.list(project_id=pid) == []  # replay queues nothing
    # the client's retry with the same key returns the one recorded result, and creates nothing new
    again = api2.post(url, body, status=(200, 201))
    assert again["job_ids"] == batches[0]["job_ids"] and again["batch_id"] == batches[0]["id"]
    assert again["family_id"] == fams[0].id and again["plan_id"] == load_plan(ctx, again["plan_id"]).id
    assert api2.post(url, body, status=(200, 201)) == again
    assert len(api2.get(f"{V2}/{pid}/jobs")["jobs"]) == 3 and len(api2.get(f"{V2}/{pid}/batches")["batches"]) == 1
    other = api2.raw("POST", url, json={**body, "idempotency_key": "jobs-vr01-2"})
    assert other.status_code == 409 and other.json()["error"]["code"] == "draft_materialized"


# --- VR02 ------------------------------------------------------------------------------------------------------------
def test_vr02_unknown_edit_submit_outcome_is_reconciled_not_resubmitted(make_api, tmp_path) -> None:
    api, engine, aux, w3d = studio_api(make_api, tmp_path)
    pid = new_project(api)
    jid = variant_jobs(api, pid, 1, "vr02")["job_ids"][0]
    item = _enhance(api, pid, jid, "vr02-a")
    engine.lose_ack = 1  # submit_edit reaches the engine, the response is lost
    _confirm(api, pid, jid, "conf-vr02-a", item)
    assert wait_until(lambda: bool(api.studio.journal.tasks.list(states=("blocked",))), 10)
    (task,) = api.studio.journal.tasks.list(states=("blocked",))
    assert task.error["retryable"] is True and len(_edit_calls(engine)) == 1
    assert api.get(f"{V2}/{pid}/jobs/{jid}")["items"][0]["tasks"]["generate"]["state"] == "blocked"
    api.post(f"/api/v1/operations/{task.id}:retry")
    api.wait_ops()
    ids = _edit_calls(engine)
    assert len(ids) == 4 and len(set(ids)) == 4  # slot 0 was found by its prompt id, not submitted again
    assert ids == [engine_prompt_id(task.id, str(i)) for i in range(4)]
    assert not [c for c in engine.calls if c[0] == "cancel"]
    done = api.studio.journal.tasks.get(task.id)
    assert done is not None and done.state == "succeeded" and done.attempts == 2
    ctx = api.studio.registry.get(pid)
    stored, _ = load_item(ctx.store, jid, _item(api, pid, jid)["id"])
    assert len(stored.candidate_sets) == 1  # the output is committed once
    cands = _item(api, pid, jid)["candidate_set"]["candidates"]
    assert len(cands) == 4 and len({c["artifact_id"] for c in cands}) == 4
    assert {c["artifact_id"] for c in cands} == {derived_id("art", task.id, str(i)) for i in range(4)}


# --- VR03 ------------------------------------------------------------------------------------------------------------
class _Crash(BaseException):
    """The process dies: not an Exception, so no handler in the stage may swallow it."""


def test_vr03_crash_after_two_of_four_slots_runs_only_the_remaining_slots(make_api, tmp_path) -> None:
    api, engine, aux, w3d = studio_api(make_api, tmp_path, coordinator=False)
    pid = new_project(api)
    jid = variant_jobs(api, pid, 1, "vr03")["job_ids"][0]
    api.post(f"{V2}/{pid}/jobs/{jid}:run", {"idempotency_key": "run-vr03-a"})
    pump(api)
    _confirm(api, pid, jid, "conf-vr03-a")
    (task,) = api.studio.journal.tasks.list(states=("queued",))
    assert api.studio.journal.tasks.claim(task.id, "pas_dead")
    real_submit, submits = engine.submit_edit, []

    def dying(req: Any) -> Any:
        submits.append(req.prompt_id)
        if len(submits) == 3:
            raise _Crash()
        return real_submit(req)
    engine.submit_edit = dying  # type: ignore[method-assign]
    with pytest.raises(_Crash):
        generate(TaskEnv(api.studio, api.studio.registry.get(pid), api.studio.journal.tasks.get(task.id)))
    saved = api.studio.journal.tasks.get(task.id).progress["engine"]  # type: ignore[union-attr]
    assert set(saved) == {"0", "1"} and all("artifact_id" in v for v in saved.values())
    assert len(_edit_calls(engine)) == 2

    engine.submit_edit = real_submit  # type: ignore[method-assign]
    api2 = restart(make_api, api, tmp_path, engine, aux, w3d)
    api2.wait_ops()
    t2 = api2.studio.journal.tasks.get(task.id)
    assert t2 is not None and t2.state == "succeeded" and t2.progress.get("reconciled_after_restart") == 1
    ids = _edit_calls(engine)
    assert len(ids) == 4 and len(set(ids)) == 4  # slots 0 and 1 were not generated again
    assert ids == [engine_prompt_id(task.id, str(i)) for i in range(4)]
    cands = _item(api2, pid, jid)["candidate_set"]["candidates"]
    assert len(cands) == 4
    assert [c["artifact_id"] for c in cands[:2]] == [saved["0"]["artifact_id"], saved["1"]["artifact_id"]]


# --- VR04 ------------------------------------------------------------------------------------------------------------
def test_vr04_qa_is_scheduled_once_when_downstream_planning_was_lost(make_api, tmp_path, monkeypatch) -> None:
    """Generation committed its candidate set and succeeded, but its follow-up tasks were never created."""
    api, engine, aux, w3d = studio_api(make_api, tmp_path)
    pid = new_project(api)
    jid = variant_jobs(api, pid, 1, "vr04")["job_ids"][0]
    item = _enhance(api, pid, jid, "vr04-a")
    real = reconcile.downstream_for

    def lost(*a: Any, **k: Any) -> list[Any]:
        raise RuntimeError("process state lost while planning the follow-up")
    monkeypatch.setattr(reconcile, "downstream_for", lost)
    _confirm(api, pid, jid, "conf-vr04-a", item)
    api.wait_ops()
    (gen,) = [t for t in api.studio.journal.tasks.list(project_id=pid) if t.stage == "generate"]
    assert gen.state == "succeeded" and _item(api, pid, jid)["candidate_set"] is not None
    assert not [t for t in api.studio.journal.tasks.list(project_id=pid) if t.family == "qa"]
    assert [t.id for t in api.studio.journal.tasks.pending_downstream()] == [gen.id]  # marked as still owed
    monkeypatch.setattr(reconcile, "downstream_for", real)

    api2 = restart(make_api, api, tmp_path, engine, aux, w3d)
    api2.wait_ops()

    def qa_stages(a: Api) -> list[str]:
        return sorted(t.stage for t in a.studio.journal.tasks.list(project_id=pid) if t.family == "qa")
    stages = qa_stages(api2)
    assert stages.count("qa_compare") == 1 and stages.count("qa_finalize") == 1 and stages.count("qa_vlm") <= 1
    assert all(t.state == "succeeded" for t in api2.studio.journal.tasks.list(project_id=pid, item_id=gen.item_id))
    assert api2.studio.journal.tasks.pending_downstream() == []
    cands = _item(api2, pid, jid)["candidate_set"]["candidates"]
    assert all(c["qa"] and "variant_change" in {r["rule_id"] for r in c["qa"]["results"]} for c in cands)
    api3 = restart(make_api, api2, tmp_path, engine, aux, w3d)  # a second restart adds nothing
    api3.wait_ops()
    assert qa_stages(api3) == stages


# --- VR05 ------------------------------------------------------------------------------------------------------------
def test_vr05_bake_failure_retry_resumes_from_raw_and_sizes_the_variant(make_api, tmp_path) -> None:
    api, engine, aux, w3d = studio_api(make_api, tmp_path)
    pid = new_project(api)
    jid = variant_jobs(api, pid, 1, "vr05", method="image_edit_reconstruct", final_height_m=3.0)["job_ids"][0]
    _confirm(api, pid, jid, "conf-vr05-a", _enhance(api, pid, jid, "vr05-a"))
    api.wait_ops()
    item = _item(api, pid, jid)
    c = item["candidate_set"]["candidates"][0]
    api.post(f"{V2}/{pid}/jobs/{jid}:approve-candidates", {"idempotency_key": "appr-vr05-a", "items": [{
        "item_id": item["id"], "expected_item_revision": item["revision"], "candidate_set_id": item["candidate_set"]["id"],
        "candidate_id": c["id"], "image_sha256": c["sha256"], "prompt_revision_id": item["candidate_set"][
            "prompt_revision_id"], "qa_evaluation_id": c["qa"]["id"] if c["qa"] else None, "override_qa": True,
        "override_reason": "test"}]})

    def build(key: str, **extra: Any) -> dict:
        it = _item(api, pid, jid)
        api.post(f"{V2}/{pid}/jobs/{jid}:build-approved", {"idempotency_key": key, "items": [{
            "item_id": it["id"], "approval_id": it["approval"], "expected_item_revision": it["revision"], **extra}]})
        api.wait_ops()
        # the run record is closed by the stage's error hook just AFTER its task turns terminal
        assert wait_until(lambda: _item(api, pid, jid)["build"]["status"] not in ("running", "queued"), 10)
        return _item(api, pid, jid)
    w3d.fail_ops["export"] = "boom"
    failed = build("build-vr05-1")["build"]
    assert failed["status"] == "failed" and "raw" in failed["artifacts"] and "size" not in failed["checkpoints"]
    assert w3d.calls.count("generate") == 1
    ok = build("build-vr05-2", mode="retry")["build"]
    assert ok["result"] == "valid" and ok["inputs"]["resumed_from"] == failed["id"]
    assert w3d.calls.count("generate") == 1  # TRELLIS.2 was not sampled again
    assert ok["artifacts"]["raw"] == failed["artifacts"]["raw"]
    checks = {x["id"]: x for x in ok["validation"]["checks"]}
    assert checks["final_height"]["ok"] and "size" in ok["checkpoints"]  # sizing ran on the retried bake
    assert {"model", "model_unsized"} <= set(ok["artifacts"])
    assert ok["artifacts"]["model"] != ok["artifacts"]["model_unsized"]


# --- VR06 ------------------------------------------------------------------------------------------------------------
def test_vr06_cancelled_edit_generation_stays_cancelled_and_a_new_confirmation_starts_fresh(make_api, tmp_path) -> None:
    api, engine, aux, w3d = studio_api(make_api, tmp_path)
    pid = new_project(api)
    jid = variant_jobs(api, pid, 1, "vr06")["job_ids"][0]
    item = _enhance(api, pid, jid, "vr06-a")
    engine.steps_to_finish = 10**9  # generation stays in flight until cancelled
    _confirm(api, pid, jid, "conf-vr06-a", item)
    (task,) = [t for t in api.studio.journal.tasks.list(project_id=pid) if t.stage == "generate"]
    assert wait_until(lambda: len(_edit_calls(engine)) == 1
                      and api.studio.journal.tasks.get(task.id).state == "running", 10)  # type: ignore[union-attr]
    old_ids = {engine_prompt_id(task.id, str(i)) for i in range(4)}
    api.post(f"{V2}/{pid}/jobs/{jid}:cancel")
    assert wait_until(lambda: api.studio.journal.tasks.get(task.id).state == "cancelled", 10)  # type: ignore[union-attr]
    assert [c[1] for c in engine.calls if c[0] == "cancel"] == [engine_prompt_id(task.id, "0")]  # only its own prompt
    assert _item(api, pid, jid)["candidate_set"] is None

    engine.steps_to_finish = 1
    api2 = restart(make_api, api, tmp_path, engine, aux, w3d)
    api2.wait_ops()
    t = api2.studio.journal.tasks.get(task.id)
    assert t is not None and t.state == "cancelled" and t.control == "cancel_requested"  # persisted, not resurrected
    assert api2.studio.journal.tasks.retry(task.id) is False
    r = api2.raw("POST", f"/api/v1/operations/{task.id}:retry")
    assert r.status_code == 409 and api2.studio.journal.tasks.get(task.id).state == "cancelled"  # type: ignore[union-attr]
    assert _item(api2, pid, jid)["candidate_set"] is None and len(_edit_calls(engine)) == 1

    res = _confirm(api2, pid, jid, "conf-vr06-b")  # the operator confirms the same prompt again
    assert res["results"][0]["ok"] and res["tasks"] and res["tasks"] != [task.id]
    api2.wait_ops()
    new = _item(api2, pid, jid)["candidate_set"]
    assert new is not None and len(new["candidates"]) == 4
    assert new["id"] != derived_id("cs", task.id) and api2.studio.journal.tasks.get(task.id).state == "cancelled"  # type: ignore[union-attr]
    new_ids = {c["engine"]["prompt_id"] for c in new["candidates"]}
    assert not new_ids & old_ids  # nothing of the cancelled attempt is reused
    assert [c[1] for c in engine.calls if c[0] == "cancel"] == [engine_prompt_id(task.id, "0")]


# --- VR07 ------------------------------------------------------------------------------------------------------------
def test_vr07_aux_release_is_not_acknowledged_while_qa_compare_is_in_flight(make_api, tmp_path) -> None:
    api, engine, aux, w3d = studio_api(make_api, tmp_path, coordinator=False)
    pid = new_project(api)
    jid = variant_jobs(api, pid, 1, "vr07")["job_ids"][0]
    api.post(f"{V2}/{pid}/jobs/{jid}:run", {"idempotency_key": "run-vr07-a"})
    pump(api)
    aux.gpu.drain_timeout = 10.0  # like the real worker: /unload waits for in-flight GPU work
    lane = api.studio.lanes["gpu1"]
    outcome: dict[str, Any] = {}

    def handoff() -> None:  # a second worker asks for the device: aux must drain and release first
        outcome["epoch"] = lane.acquire("worker3d")
        outcome["at"] = time.monotonic()

    def during(n: int) -> None:
        if n != 4:  # the last of the four candidate comparisons
            return
        assert aux.gpu.active == 1 and aux.loaded["vlm"] is True
        th = threading.Thread(target=handoff)
        outcome["thread"] = th
        th.start()
        time.sleep(0.4)
        outcome["early"] = "epoch" in outcome  # acknowledged while the comparison is still running?
        outcome["loaded_while_active"] = aux.loaded["vlm"]
    aux.during_compare = during
    returned: list[float] = []
    real_compare = aux.compare

    def timed(**kw: Any) -> Any:
        res = real_compare(**kw)
        returned.append(time.monotonic())
        return res
    aux.compare = timed  # type: ignore[method-assign]
    _confirm(api, pid, jid, "conf-vr07-a")
    pump(api)
    outcome["thread"].join(10)
    assert outcome["early"] is False and outcome["loaded_while_active"] is True
    assert "epoch" in outcome and lane.owner == "worker3d" and aux.gpu.active == 0
    assert aux.loaded == {"vlm": False, "birefnet": False} and aux.gpu.admitting is False
    (cmp_task,) = [t for t in api.studio.journal.tasks.list(project_id=pid) if t.stage == "qa_compare"]
    assert cmp_task.state == "succeeded" and aux.calls.count("compare") == 4
    assert len(returned) == 4 and outcome["at"] >= returned[-1]  # acknowledged only after the comparison returned


# --- VR08 ------------------------------------------------------------------------------------------------------------
def test_vr08_corrupt_reference_fails_its_item_only_and_the_lane_keeps_serving(make_api, tmp_path) -> None:
    api, engine, aux, w3d = studio_api(make_api, tmp_path)
    pid = new_project(api)
    bad = variant_jobs(api, pid, 1, "vr08a", color=(200, 80, 40))
    good = variant_jobs(api, pid, 1, "vr08b", color=(30, 140, 60))
    jobs = [bad["job_ids"][0], good["job_ids"][0]]
    rid = _start(api, pid, _batch(api, pid, jobs, "batch-vr08-1"), "start-vr08-1")["run_id"]
    api.wait_ops()
    ctx = api.studio.registry.get(pid)
    primary = next(r for r in load_plan(ctx, bad["plan_id"]).references if r.role == "primary")
    blob = ctx.store.repo._blob_path(primary.sha256)  # type: ignore[attr-defined]
    data = bytearray(blob.read_bytes())
    data[0] ^= 0xFF
    blob.chmod(0o644)
    blob.write_bytes(bytes(data))
    items = sorted(_items(_run(api, pid, rid)), key=lambda i: jobs.index(i["job_id"]))  # the corrupt Job goes first
    assert all(i["current_prompt"] for i in items)
    _confirm_wave(api, pid, rid, items, "wave-vr08-1")
    api.wait_ops()
    gen = {t.job_id: t for t in api.studio.journal.tasks.list(project_id=pid) if t.stage == "generate"}
    assert gen[jobs[0]].state == "failed" and gen[jobs[0]].error["code"] == "source_integrity_failed"
    assert gen[jobs[1]].state == "succeeded"
    assert _item(api, pid, jobs[0])["candidate_set"] is None
    assert len(_item(api, pid, jobs[1])["candidate_set"]["candidates"]) == 4
    assert len(_edit_calls(engine)) == 4  # only the healthy Job reached the engine
    assert not api.studio.journal.tasks.list(states=("blocked",))
    # The wave's tasks are queued one by one, so a pass may start before the second exists: the pass count is
    # scheduling noise, but the lane must have served both under one residency without ever being blocked.
    passes = [p for p in api.get("/api/v2/passes", params={"lane": "gpu0", "limit": 20})["passes"] if p["task_ids"]]
    assert {t for p in passes for t in p["task_ids"]} == {gen[j].id for j in jobs}
    assert len({p["residency"] for p in passes}) == 1 and not any(p["measured"]["switched"] for p in passes)
    assert {p["close_reason"] for p in passes} == {"exhausted"}


def test_vr06b_cancel_pending_at_crash_stops_the_orphaned_engine_prompt(make_api, tmp_path) -> None:
    """A crash while a cancel was pending: on restart the task is cancelled AND its own unfinished engine prompts
    are cancelled by exact id (the engine would otherwise keep running them)."""
    api, engine, aux, w3d = studio_api(make_api, tmp_path)
    pid = new_project(api)
    jid = variant_jobs(api, pid, 1, "vr06b")["job_ids"][0]
    item = _enhance(api, pid, jid, "vr06b")
    api.c.__exit__(None, None, None)  # no coordinator from here: the state below is what a crash leaves behind
    api = make_api(studio=build_studio(make_settings(tmp_path), engine, aux, w3d), coordinator=False)
    _confirm(api, pid, jid, "conf-vr06b", item)
    (task,) = [t for t in api.studio.journal.tasks.list(project_id=pid) if t.stage == "generate"]
    ids = [engine_prompt_id(task.id, str(i)) for i in range(4)]
    slots = {"0": {"prompt_id": ids[0], "submitted": True, "artifact_id": "art_done"},  # finished: left alone
             "1": {"prompt_id": ids[1], "submitted": True}}  # in flight on the engine
    ts = api.studio.journal.tasks
    with ts._lock:
        ts._db.execute("UPDATE stage_tasks SET state='running', control='cancel_requested', progress=? WHERE id=?",
                       (json.dumps({"engine": slots}), task.id))
    api2 = restart(make_api, api, tmp_path, engine, aux, w3d)
    t = api2.studio.journal.tasks.get(task.id)
    assert t is not None and t.state == "cancelled"
    assert [c[1] for c in engine.calls if c[0] == "cancel"] == [ids[1]]  # only its own unfinished prompt
