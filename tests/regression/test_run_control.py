"""R02: persisted run-level intent is enforced at every admission (create, ready, claim, waves)."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from assetstudio_server.adapters.fake import FakeAux, FakeEngine, FakeWorker3d
from assetstudio_server.coordinator.runner import Coordinator
from assetstudio_server.coordinator.stages import STAGES
from assetstudio_server.services import runs
from assetstudio_server.studio import build_studio

from tests.conftest import Api, make_settings
from tests.contract.test_jobs_batches import (
    V2,
    _batch,
    _confirm_wave,
    _items,
    _job,
    _run,
    _setup,
    _start,
    _studio,
)
from tests.contract.test_variant_scheduling import pump

QA = ("mask", "qa_vlm", "qa_finalize", "qa_compare")


def _at_gate(make_api, tmp_path: Path, **kw: Any) -> tuple[Api, str, str, str]:
    api, *_ = _studio(make_api, tmp_path, **kw)
    pid = _setup(api)
    job = _job(api, pid, "icons", ["Sword"], "job-rc-1", candidate_count=1)
    rid = _start(api, pid, _batch(api, pid, [job], "batch-rc-1"), "start-rc-1")["run_id"]
    pump(api)
    return api, pid, rid, job


def _qa(api: Api) -> list[Any]:
    return [t for t in api.studio.journal.tasks.list() if t.stage in QA]


def test_pause_while_generate_runs_admits_qa_paused_until_resume(make_api, tmp_path) -> None:
    api, pid, rid, _ = _at_gate(make_api, tmp_path, coordinator=False)
    _confirm_wave(api, pid, rid, _items(_run(api, pid, rid)), "wave-rc-1")
    gen = STAGES["generate"]

    def pausing(env: Any) -> dict[str, Any]:
        api.post(f"{V2}/{pid}/runs/{rid}:pause")
        return gen.run(env)
    stages = {**STAGES, "generate": replace(gen, run=pausing)}
    coord = pump(api, coord=Coordinator(api.studio, stages))
    tasks = api.studio.journal.tasks
    assert [t.state for t in tasks.list(run_id=rid) if t.stage == "generate"] == ["succeeded"]
    qa = _qa(api)
    assert qa and all(t.control == "paused" and t.state == "queued" for t in qa)
    assert not any(t.state == "running" for t in tasks.list(run_id=rid))
    assert _run(api, pid, rid)["status"] == "paused"
    api.post(f"{V2}/{pid}/runs/{rid}:resume")
    pump(api, coord=coord)
    assert all(t.state == "succeeded" for t in _qa(api))


def test_pause_at_human_gate_survives_restart_and_wave_tasks_are_paused(make_api, tmp_path) -> None:
    api, pid, rid, _ = _at_gate(make_api, tmp_path, coordinator=False)
    assert api.studio.journal.tasks.list(run_id=rid, states=("queued", "running", "blocked")) == []
    assert api.post(f"{V2}/{pid}/runs/{rid}:pause")["status"] == "paused"
    api.c.__exit__(None, None, None)
    api2 = make_api(coordinator=False, studio=build_studio(make_settings(tmp_path), FakeEngine(), FakeAux(),
                                                           FakeWorker3d()))
    run = _run(api2, pid, rid)
    assert run["status"] == "paused" and run["control"] == "paused" and run["control_revision"] >= 1
    _confirm_wave(api2, pid, rid, _items(run), "wave-rc-2")  # human decisions are accepted while paused
    gens = [t for t in api2.studio.journal.tasks.list(run_id=rid) if t.stage == "generate"]
    assert gens and all(t.control == "paused" for t in gens)
    pump(api2)
    assert all(api2.studio.journal.tasks.get(t.id).state == "queued" for t in gens)
    api2.post(f"{V2}/{pid}/runs/{rid}:resume")
    assert api2.raw("POST", f"{V2}/{pid}/runs/{rid}:resume").status_code == 409


def test_closed_run_rejects_waves_and_foreign_items_are_refused_before_effects(make_api, tmp_path) -> None:
    api, pid, rid, _ = _at_gate(make_api, tmp_path, coordinator=False)
    other = _job(api, pid, "props", ["Crate"], "job-rc-2", candidate_count=1)
    body = {"idempotency_key": "wave-rc-3", "items": [
        {"job_id": other, "item_id": "itm_" + "0" * 16, "prompt_revision_id": "x", "expected_item_revision": 1}]}
    r = api.raw("POST", f"{V2}/{pid}/runs/{rid}:confirm-prompts", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_in_run"
    body["items"][0].pop("job_id")
    r = api.raw("POST", f"{V2}/{pid}/runs/{rid}:confirm-prompts", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "job_required"
    assert not [t for t in api.studio.journal.tasks.list() if t.stage == "generate"]
    items = _items(_run(api, pid, rid))
    assert api.post(f"{V2}/{pid}/runs/{rid}:close")["status"] == "closed"
    r = api.raw("POST", f"{V2}/{pid}/runs/{rid}:confirm-prompts", json={"idempotency_key": "wave-rc-4", "items": [
        {"job_id": i["job_id"], "item_id": i["id"], "prompt_revision_id": i["current_prompt"],
         "expected_item_revision": i["revision"]} for i in items]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "run_not_open"
    assert not [t for t in api.studio.journal.tasks.list() if t.stage == "generate"]


def test_cancelled_run_status_and_job_actions_run_standalone(make_api, tmp_path) -> None:
    api, pid, rid, job = _at_gate(make_api, tmp_path, coordinator=False)
    ctx = api.studio.registry.get(pid)
    assert runs.active_run_for(api.studio, ctx, job) == rid
    assert api.post(f"{V2}/{pid}/runs/{rid}:cancel")["status"] == "cancelled"
    assert runs.active_run_for(api.studio, ctx, job) is None
    assert api.raw("POST", f"{V2}/{pid}/runs/{rid}:resume").status_code == 409
    r = api.raw("POST", f"{V2}/{pid}/jobs/{job}:run", json={"idempotency_key": "run-standalone-rc"})
    assert r.status_code in (200, 201, 202), r.text
    assert all(t.run_id is None for t in api.studio.journal.tasks.list() if t.state == "queued")
