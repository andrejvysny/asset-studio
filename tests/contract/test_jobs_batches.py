"""Jobs + Batches with stage-first cross-Job scheduling (JB01–JB16, RI04, RI05, IM10). SIMULATED engines only:
contract evidence for grouping/gates/recovery, never GPU or model-quality evidence."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from assetstudio_server.adapters.fake import FakeAux, FakeEngine, FakeWorker3d
from assetstudio_server.coordinator.runner import Coordinator, Limits
from assetstudio_server.services import commands
from assetstudio_server.studio import build_studio

from tests.conftest import Api, make_settings, new_project

V2 = "/api/v2/projects"
CATEGORIES = [
    {"id": "concept", "slug": "concept", "label": "Concept", "defaults": {"kind": "concept_art"}},
    {"id": "props", "slug": "props", "label": "Props", "defaults": {"kind": "model3d"}},
    {"id": "icons", "slug": "icons", "label": "Icons", "defaults": {"kind": "icon"}},
]


def _setup(api: Api) -> str:
    pid = new_project(api)
    cfg = api.get(f"/api/v1/projects/{pid}/config")["config"]
    cfg["categories"] = CATEGORIES
    r = api.raw("PATCH", f"/api/v1/projects/{pid}/config", json={"expected_revision": 1, "config": cfg})
    assert r.status_code == 200, r.text
    return pid


def _job(api: Api, pid: str, cat: str, names: list[str], key: str, **kw: Any) -> str:
    out = api.post(f"{V2}/{pid}/jobs", {"title": f"{cat} job", "category_id": cat, "idempotency_key": key,
                                        "items": [{"name": n, "brief": f"brief for {n}"} for n in names], **kw})
    return out["job"]["id"]


def _studio(make_api, tmp_path: Path, **kw: Any) -> tuple[Api, FakeEngine, FakeAux, FakeWorker3d]:
    engine, aux, w3d = FakeEngine(), FakeAux(), FakeWorker3d()
    api = make_api(studio=build_studio(make_settings(tmp_path), engine, aux, w3d), **kw)
    return api, engine, aux, w3d


def _three_jobs(api: Api, pid: str) -> list[str]:
    return [_job(api, pid, "props", ["Crate", "Barrel"], "job-props-1", candidate_count=2),
            _job(api, pid, "icons", ["Sword", "Shield"], "job-icons-1", candidate_count=2),
            _job(api, pid, "concept", ["Tavern", "Harbour"], "job-concept-1", candidate_count=2)]


def _batch(api: Api, pid: str, jobs: list[str], key: str = "batch-create-1") -> str:
    return api.post(f"{V2}/{pid}/batches", {"title": "Production pass", "job_ids": jobs,
                                           "idempotency_key": key})["batch"]["id"]


def _start(api: Api, pid: str, bid: str, key: str = "batch-start-1") -> dict:
    plan = api.post(f"{V2}/{pid}/batches/{bid}:plan", {})
    return api.post(f"{V2}/{pid}/batches/{bid}:start", {"plan_id": plan["plan_id"],
                                                        "plan_sha256": plan["plan_sha256"],
                                                        "idempotency_key": key})


def _run(api: Api, pid: str, rid: str) -> dict:
    return api.get(f"{V2}/{pid}/runs/{rid}")


def _items(run: dict) -> list[dict]:
    return [dict(i, job_id=j["id"]) for j in run["jobs"] for i in j["items"]]


def _passes(api: Api, lane: str) -> list[dict]:
    return list(reversed(api.get("/api/v2/passes", params={"lane": lane, "limit": 500})["passes"]))


def test_saving_jobs_and_batch_runs_no_inference(make_api, tmp_path) -> None:
    """JB01 + JB02: several multi-item Jobs + a Batch = zero tasks, zero model calls; items keep ids/seeds."""
    api, engine, aux, _ = _studio(make_api, tmp_path)
    pid = _setup(api)
    jobs = _three_jobs(api, pid)
    before = {j: api.get(f"{V2}/{pid}/jobs/{j}") for j in jobs}
    bid = _batch(api, pid, jobs)
    assert api.studio.journal.tasks.list() == [] and aux.calls == [] and engine.calls == []
    b = api.get(f"{V2}/{pid}/batches/{bid}")
    assert b["jobs"] == 3 and b["items"] == 6 and set(b["kinds"]) == {"model3d", "icon", "concept_art"}
    for j in jobs:
        after = api.get(f"{V2}/{pid}/jobs/{j}")
        assert [i["id"] for i in after["items"]] == [i["id"] for i in before[j]["items"]]
        assert after["seed_family"] == before[j]["seed_family"]
    r = api.raw("POST", f"{V2}/{pid}/batches", json={"title": "dup", "job_ids": [jobs[0], jobs[0]],
                                                     "idempotency_key": "batch-dup-1"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "duplicate_jobs"


def test_batch_start_groups_enhancement_across_jobs_and_stops_at_review(make_api, tmp_path) -> None:
    """JB03 + JB05 + JB08: mixed kinds; one VLM residency serves all Jobs; no image task before confirmation."""
    api, engine, aux, _ = _studio(make_api, tmp_path)
    pid = _setup(api)
    jobs = _three_jobs(api, pid)
    bid = _batch(api, pid, jobs)
    start = _start(api, pid, bid)
    api.wait_ops()
    run = _run(api, pid, start["run_id"])
    assert run["counts"]["items"] == 6 and run["counts"]["enhanced"] == 6
    assert run["status"] == "waiting_for_review" and engine.calls == []
    enh = [p for p in _passes(api, "gpu1") if p["residency"].startswith("aux.vlm")]
    assert len(enh) == 1 and set(enh[0]["jobs"]) == set(jobs) and len(enh[0]["task_ids"]) == 6
    assert enh[0]["measured"]["model_loads"] == {"vlm": 1, "birefnet": 0}  # one load, no reload per Job
    assert not any(t.stage == "generate" for t in api.studio.journal.tasks.list())


def test_same_plan_twice_is_one_run_and_jobs_have_one_owner(make_api, tmp_path) -> None:
    """JB04: replaying a start (same or new key) returns the same run; a Job in an active run cannot start again."""
    api, *_ = _studio(make_api, tmp_path, coordinator=False)
    pid = _setup(api)
    jobs = _three_jobs(api, pid)
    bid = _batch(api, pid, jobs)
    plan = api.post(f"{V2}/{pid}/batches/{bid}:plan", {})
    body = {"plan_id": plan["plan_id"], "plan_sha256": plan["plan_sha256"]}
    a = api.post(f"{V2}/{pid}/batches/{bid}:start", {**body, "idempotency_key": "start-a-1"})
    b = api.post(f"{V2}/{pid}/batches/{bid}:start", {**body, "idempotency_key": "start-b-1"})
    assert a["run_id"] == b["run_id"]
    assert len([t for t in api.studio.journal.tasks.list() if t.stage == "enhance"]) == 6
    r = api.raw("POST", f"{V2}/{pid}/jobs/{jobs[0]}:run", json={"idempotency_key": "run-standalone-1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "job_in_active_run"


def _confirm_wave(api: Api, pid: str, rid: str, items: list[dict], key: str) -> dict:
    return api.post(f"{V2}/{pid}/runs/{rid}:confirm-prompts", {"idempotency_key": key, "items": [
        {"job_id": i["job_id"], "item_id": i["id"], "prompt_revision_id": i["current_prompt"],
         "expected_item_revision": i["revision"]} for i in items]})


def test_cross_job_confirmation_generates_selected_items_in_one_residency(make_api, tmp_path) -> None:
    """JB06 + JB09 + JB10 (QA): selected prompts across Jobs, one image residency, grouped mask/VLM passes."""
    api, engine, aux, _ = _studio(make_api, tmp_path)
    pid = _setup(api)
    jobs = _three_jobs(api, pid)
    bid = _batch(api, pid, jobs)
    rid = _start(api, pid, bid)["run_id"]
    api.wait_ops()
    items = _items(_run(api, pid, rid))
    chosen = [i for i in items if i["name"] != "Harbour"]  # one row deliberately left unconfirmed
    out = _confirm_wave(api, pid, rid, chosen, "wave-confirm-1")
    assert all(r["ok"] for r in out["results"]) and len(out["tasks"]) == 5
    api.wait_ops()
    run = _run(api, pid, rid)
    by_name = {i["name"]: i for i in _items(run)}
    assert by_name["Harbour"]["candidate_set"] is None and by_name["Harbour"]["prompt_confirmed"] is None
    assert all(by_name[n]["candidate_set"] for n in ("Crate", "Barrel", "Sword", "Shield", "Tavern"))
    gen = [p for p in _passes(api, "gpu0")]
    assert len(gen) == 1 and len(gen[0]["task_ids"]) == 5 and set(gen[0]["jobs"]) == set(jobs)
    assert gen[0]["measured"]["model_loads"] is None  # ComfyUI reports no load counter: unavailable, not 0
    qa = [p for p in _passes(api, "gpu1") if p["residency"].startswith("aux.vlm") and
          any(api.studio.journal.tasks.get(t).stage == "qa_vlm" for t in p["task_ids"])]
    assert len(qa) == 1 and set(qa[0]["jobs"]) == set(jobs)  # one cross-Job VLM QA pass, not per candidate
    assert run["waves"] and len(run["waves"]) == 1


def test_different_speed_profiles_split_image_passes(make_api, tmp_path) -> None:
    """JB07: residency identity uses the exact weight modifiers; different profiles never share a pass."""
    api, *_ = _studio(make_api, tmp_path)
    pid = _setup(api)
    cfg = api.get(f"/api/v1/projects/{pid}/config")["config"]
    cfg["pipelines"] = {"icon.default": {"parameters": {"speed_preset": "lightning_8step"}}}
    r = api.raw("PATCH", f"/api/v1/projects/{pid}/config", json={"expected_revision": 2, "config": cfg})
    assert r.status_code == 200, r.text
    a = _job(api, pid, "concept", ["Tavern"], "job-speed-a", candidate_count=1)
    b = _job(api, pid, "icons", ["Sword"], "job-speed-b", candidate_count=1)
    rid = _start(api, pid, _batch(api, pid, [a, b], "batch-speed-1"), "start-speed-1")["run_id"]
    api.wait_ops()
    _confirm_wave(api, pid, rid, _items(_run(api, pid, rid)), "wave-speed-1")
    tasks = [t for t in api.studio.journal.tasks.list() if t.stage == "generate"]
    assert len({t.residency for t in tasks}) == 2
    assert any("lightning_8step@" in t.residency for t in tasks)


def _approve_all(api: Api, pid: str, rid: str, key: str, names: set[str] | None = None) -> None:
    items = [i for i in _items(_run(api, pid, rid)) if i["candidate_set"] and (names is None or i["name"] in names)]
    body = {"idempotency_key": key, "items": []}
    for i in items:
        c = i["candidate_set"]["candidates"][0]
        body["items"].append({"job_id": i["job_id"], "item_id": i["id"], "expected_item_revision": i["revision"],
                              "candidate_set_id": i["candidate_set"]["id"], "candidate_id": c["id"],
                              "image_sha256": c["sha256"], "prompt_revision_id": i["candidate_set"]["prompt_revision_id"],
                              "qa_evaluation_id": c["qa"]["id"] if c["qa"] else None, "override_qa": True})
    res = api.post(f"{V2}/{pid}/runs/{rid}:approve-candidates", body)
    assert all(r["ok"] for r in res["results"]), res


def test_grouped_3d_builds_do_not_thrash_gpu1_and_other_items_continue(make_api, tmp_path) -> None:
    """JB10 + JB11 + JB12: two 3D items segment/sample/bake in grouped passes; GPU1 passes never overlap; an
    isolated generation failure leaves every other item/Job running."""
    api, engine, aux, w3d = _studio(make_api, tmp_path)
    pid = _setup(api)
    jobs = _three_jobs(api, pid)
    rid = _start(api, pid, _batch(api, pid, jobs))["run_id"]
    api.wait_ops()
    _confirm_wave(api, pid, rid, _items(_run(api, pid, rid)), "wave-confirm-3d")
    api.wait_ops()
    _approve_all(api, pid, rid, "wave-approve-3d")
    items = _items(_run(api, pid, rid))
    w3d.fail_ops["generate"] = "input_invalid"  # the first sampled 3D item fails; the second must still build
    body = {"idempotency_key": "wave-build-3d", "items": [
        {"job_id": i["job_id"], "item_id": i["id"], "approval_id": i["approval"],
         "expected_item_revision": i["revision"]} for i in items if i["approval"]]}
    api.post(f"{V2}/{pid}/runs/{rid}:build-approved", body)
    api.wait_ops()
    run = _run(api, pid, rid)
    builds = {i["name"]: i["build"] for i in _items(run) if i["build"]}
    failed = [n for n, b in builds.items() if b["status"] == "failed"]
    assert len(failed) == 1 and failed[0] in ("Crate", "Barrel")
    assert all(b["result"] == "valid" for n, b in builds.items() if n not in failed)
    assert run["counts"]["builds_valid"] == 5 and run["counts"]["builds_failed"] == 1
    gpu1 = _passes(api, "gpu1")
    order = [p["residency"].split(":")[0] for p in gpu1 if p["task_ids"]]
    assert order.count("worker3d.trellis") == 1 and order.count("worker3d.bake") == 1  # grouped, not per item
    grants = [e for e in api.studio.lanes["gpu1"].history if e["event"] == "granted"]
    assert len(grants) <= 4  # aux -> worker3d (-> aux for icon masks) handoffs, never per item
    spans = sorted((p["started_at"], p["ended_at"]) for p in gpu1)
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:], strict=False))  # JB11: no GPU1 overlap


def test_membership_changes_affect_only_later_runs_and_partial_close(make_api, tmp_path) -> None:
    """JB13 + JB14: a started run keeps its frozen selection; closing keeps undecided work; a later run skips
    committed work."""
    api, engine, *_ = _studio(make_api, tmp_path)
    pid = _setup(api)
    jobs = _three_jobs(api, pid)
    bid = _batch(api, pid, jobs[:2])
    rid = _start(api, pid, bid)["run_id"]
    api.wait_ops()
    b = api.get(f"{V2}/{pid}/batches/{bid}")
    api.raw("PATCH", f"{V2}/{pid}/batches/{bid}", json={"expected_revision": b["revision"], "job_ids": jobs})
    assert set(_run(api, pid, rid)["job_ids"]) == set(jobs[:2])
    r = api.post(f"{V2}/{pid}/runs/{rid}:close")
    assert r["status"] == "closed"
    rid2 = _start(api, pid, bid, "batch-start-2")["run_id"]
    api.wait_ops()
    run2 = _run(api, pid, rid2)
    assert rid2 != rid and set(run2["job_ids"]) == set(jobs)
    enh = [t for t in api.studio.journal.tasks.list(run_id=rid2) if t.stage == "enhance"]
    assert len(enh) == 2  # only the newly added Job's items; already enhanced prompts are not redone


def test_pass_fairness_bounds_consecutive_passes(make_api, tmp_path) -> None:
    """JB15: with other work waiting, the resident group yields after max_consecutive_passes."""
    api, *_ = _studio(make_api, tmp_path, coordinator=False)
    from assetstudio_server.taskstore import NewTask

    tasks = [NewTask(project_id="prj_0000000000000000", job_id=f"job_{j:0>16}", item_id=f"itm_{n:0>16}",
                     stage="derive", family="build", input_key=str(n), inputs={}, lane="cpu", residency=res)
             for n, (j, res) in enumerate([(1, "cpu.a")] * 6 + [(2, "cpu.b")])]
    api.studio.journal.tasks.create(tasks, "cmd_x")
    c = Coordinator(api.studio, limits=Limits(max_tasks_per_pass=1, max_consecutive_passes=2))
    picks = []
    for _ in range(3):
        res, group = c.choose("cpu")
        picks.append(res)
        api.studio.journal.tasks.claim(group[0].id, "pas_x")
        api.studio.journal.tasks.complete(group[0].id, {}, [])
    assert picks == ["cpu.a", "cpu.a", "cpu.b"]


def test_crash_after_command_intent_replays_exactly_once(make_api, tmp_path, monkeypatch) -> None:
    """RI04: the intent is durable before effects; restart replays it; the client retry returns the same result."""
    api, *_ = _studio(make_api, tmp_path)
    pid = _setup(api)
    jid = _job(api, pid, "concept", ["Tavern", "Harbour"], "job-crash-1", candidate_count=1)
    api.post(f"{V2}/{pid}/jobs/{jid}:run", {"idempotency_key": "run-crash-1"})
    api.wait_ops()
    items = api.get(f"{V2}/{pid}/jobs/{jid}")["items"]
    real = commands.REPLAY["confirm_and_generate"]

    def crash(*a, **k):
        raise RuntimeError("process died after the intent")
    monkeypatch.setitem(commands.REPLAY, "confirm_and_generate", crash)
    body = {"idempotency_key": "confirm-crash-1", "items": [
        {"item_id": i["id"], "prompt_revision_id": i["current_prompt"], "expected_item_revision": i["revision"]}
        for i in items]}
    with pytest.raises(RuntimeError):
        api.c.post(f"{V2}/{pid}/jobs/{jid}:confirm-and-generate", json=body, headers={"x-assetstudio": "1"})
    assert not [t for t in api.studio.journal.tasks.list() if t.stage == "generate"]
    monkeypatch.setitem(commands.REPLAY, "confirm_and_generate", real)
    api.c.__exit__(None, None, None)
    api2 = make_api(studio=build_studio(make_settings(tmp_path), FakeEngine(), FakeAux(), FakeWorker3d()))
    api2.wait_ops()
    gen = [t for t in api2.studio.journal.tasks.list() if t.stage == "generate"]
    assert len(gen) == 2
    again = api2.post(f"{V2}/{pid}/jobs/{jid}:confirm-and-generate", body)
    assert sorted(again["tasks"]) == sorted(t.id for t in gen)
    assert len([t for t in api2.studio.journal.tasks.list() if t.stage == "generate"]) == 2


def test_crash_between_candidate_commit_and_qa_creates_qa_once(make_api, tmp_path) -> None:
    """RI05: generation committed its candidate set, the process died before completing the task; after restart
    the task reconciles and its QA exists exactly once."""
    engine, aux, w3d = FakeEngine(), FakeAux(), FakeWorker3d()
    api = make_api(coordinator=False, studio=build_studio(make_settings(tmp_path), engine, aux, w3d))
    pid = _setup(api)
    jid = _job(api, pid, "concept", ["Tavern"], "job-ri05-1", candidate_count=2)
    api.post(f"{V2}/{pid}/jobs/{jid}:edit-prompts", {"items": [
        {"item_id": i["id"], "description": "a tavern", "expected_item_revision": i["revision"]}
        for i in api.get(f"{V2}/{pid}/jobs/{jid}")["items"]]})
    it = api.get(f"{V2}/{pid}/jobs/{jid}")["items"][0]
    api.post(f"{V2}/{pid}/jobs/{jid}:confirm-and-generate", {"idempotency_key": "confirm-ri05-1", "items": [
        {"item_id": it["id"], "prompt_revision_id": it["current_prompt"], "expected_item_revision": it["revision"]}]})
    from assetstudio_server.coordinator.runner import TaskEnv
    from assetstudio_server.coordinator.stages.generate import generate

    task = api.studio.journal.tasks.list(states=("queued",))[0]
    assert api.studio.journal.tasks.claim(task.id, "pas_dead")
    generate(TaskEnv(api.studio, api.studio.registry.get(pid), api.studio.journal.tasks.get(task.id)))
    assert api.get(f"{V2}/{pid}/jobs/{jid}")["items"][0]["candidate_set"] is not None  # committed ... then crash
    api.c.__exit__(None, None, None)
    api2 = make_api(studio=build_studio(make_settings(tmp_path), engine, aux, w3d))
    api2.wait_ops()
    finals = [t for t in api2.studio.journal.tasks.list() if t.stage == "qa_finalize"]
    assert len(finals) == 1 and finals[0].state == "succeeded"
    item = api2.get(f"{V2}/{pid}/jobs/{jid}")["items"][0]
    assert all(c["qa"] for c in item["candidate_set"]["candidates"])
    assert sum(1 for c in engine.calls if c[0] == "submit") == 2  # the replayed task did not resubmit


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "projects"


def test_legacy_batches_are_jobs_not_groups(make_api, tmp_path) -> None:
    """IM10: v1 batch ids resolve as Jobs in v2; no legacy record is reinterpreted as a grouping Batch."""
    api: Api = make_api()
    root = tmp_path / "projects" / "legacy"
    shutil.copytree(FIXTURES / "60ff832" / "project", root)
    pid = api.post("/api/v1/projects:register", {"root": str(root)})["id"]
    jobs = api.get(f"{V2}/{pid}/jobs")["jobs"]
    assert len(jobs) == 4 and all(j["id"].startswith("bat_") and j["legacy"] for j in jobs)
    assert api.get(f"{V2}/{pid}/batches")["batches"] == []
    detail = api.get(f"{V2}/{pid}/jobs/{jobs[0]['id']}")
    assert detail["items"] and all(i["tasks"] is not None for i in detail["items"])
