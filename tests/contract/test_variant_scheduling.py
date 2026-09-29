"""Variant scheduling + migration acceptance (VS01-VS04, VM01-VM03). SIMULATED engines only: contract evidence for
grouping, lane independence and legacy compatibility, never GPU or model-quality evidence."""
from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any

import pytest
from assetstudio_server.adapters.fake import FakeAux, FakeEngine, FakeWorker3d
from assetstudio_server.coordinator.runner import Coordinator, wait_until
from assetstudio_server.studio import build_studio

from tests.conftest import Api, make_settings, new_project
from tests.contract.test_api_library import _glb, _import
from tests.contract.test_api_variants import _create, _draft, _png_src, _prepare, _rows
from tests.contract.test_jobs_batches import _batch, _confirm_wave, _items, _job, _passes, _run, _setup, _start

P = "/api/v1/projects"
V2 = "/api/v2/projects"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "projects"
RESIZE = {"op": "resize_keep_aspect", "max_width": 32, "max_height": 32}


def studio_api(make_api: Any, tmp_path: Path, **kw: Any) -> tuple[Api, FakeEngine, FakeAux, FakeWorker3d]:
    engine, aux, w3d = FakeEngine(), FakeAux(), FakeWorker3d()
    return make_api(studio=build_studio(make_settings(tmp_path), engine, aux, w3d), **kw), engine, aux, w3d


def pump(api: Api, lanes: tuple[str, ...] = ("gpu1", "gpu0", "cpu"), coord: Coordinator | None = None) -> Coordinator:
    """Deterministic single-threaded scheduler: run passes on the given lanes until nothing is ready."""
    coord = coord or Coordinator(api.studio)
    progressed = True
    while progressed:
        progressed = False
        for lane in lanes:
            picked = coord.choose(lane)
            if picked is not None:
                coord.run_pass(lane, *picked)
                progressed = True
    return coord


def variant_jobs(api: Api, pid: str, rows: int, key: str, method: str = "image_edit",
                 color: tuple[int, int, int] = (200, 80, 40), **row: Any) -> dict:
    src = _png_src(api, pid, f"Tile{key}", color) if method == "image_edit" else _import(
        api, pid, f"Crate{key}", _glb(), "crate.glb")
    d = _draft(api, pid, src, method, f"draft-{key}", rows=_rows(rows, **row), requested_variants=rows)
    return {**_create(api, pid, _prepare(api, pid, d), f"jobs-{key}"), "source": src}


# --- VS01 / VS02: one edit residency for every variant Job ------------------------------------------------------------
def test_vs01_edit_tasks_of_all_variant_jobs_share_one_residency_pass(make_api, tmp_path) -> None:
    api, engine, aux, _ = studio_api(make_api, tmp_path, coordinator=False)
    pid = _setup(api)
    out = variant_jobs(api, pid, 3, "vs01")
    t2i = _job(api, pid, "concept", ["Tavern"], "job-vs01-t2i", candidate_count=2)
    # the T2I Job sits between the edit Jobs: a FIFO-by-Job scheduler would switch models three times
    jobs = [out["job_ids"][0], t2i, out["job_ids"][1], out["job_ids"][2]]
    rid = _start(api, pid, _batch(api, pid, jobs, "batch-vs01-1"), "start-vs01-1")["run_id"]
    pump(api)
    items = sorted(_items(_run(api, pid, rid)), key=lambda i: jobs.index(i["job_id"]))
    assert len(items) == 4 and all(i["current_prompt"] for i in items) and engine.calls == []
    _confirm_wave(api, pid, rid, items, "wave-vs01-1")
    gen = [t for t in api.studio.journal.tasks.list(project_id=pid) if t.stage == "generate"]
    assert [t.job_id for t in gen] == jobs
    pump(api, ("gpu0",))
    passes = [p for p in reversed(api.get("/api/v2/passes", params={"lane": "gpu0", "limit": 50})["passes"])
              if p["task_ids"]]
    edit_res = {t.residency for t in gen if t.job_id != t2i}
    (t2i_res,) = {t.residency for t in gen if t.job_id == t2i}
    (edit_key,) = edit_res
    assert edit_key.startswith("comfyui:qwen_image_edit_2511@") and edit_key.endswith("|edit")
    assert [p["residency"] for p in passes] == [edit_key, t2i_res]  # one pass per model, edit work first
    assert set(passes[0]["jobs"]) == set(jobs) - {t2i} and len(passes[0]["task_ids"]) == 3
    assert sum(1 for p in passes if p["measured"]["switched"]) <= 2  # spec bound; grouping needs exactly one
    assert sum(1 for c in engine.calls if c[0] == "submit_edit") == 12
    assert sum(1 for c in engine.calls if c[0] == "submit") == 2


def test_vs02_residency_ignores_source_and_row_but_task_inputs_differ(make_api, tmp_path) -> None:
    api, *_ = studio_api(make_api, tmp_path, coordinator=False)
    pid = new_project(api)
    a = variant_jobs(api, pid, 2, "vs02a", color=(200, 80, 40))
    b = variant_jobs(api, pid, 1, "vs02b", color=(30, 140, 60))
    ids = [*a["job_ids"], *b["job_ids"]]
    for n, jid in enumerate(ids):
        api.post(f"{V2}/{pid}/jobs/{jid}:run", {"idempotency_key": f"run-vs02-{n}"})
    pump(api)
    for n, jid in enumerate(ids):
        it = api.get(f"{V2}/{pid}/jobs/{jid}")["items"][0]
        api.post(f"{V2}/{pid}/jobs/{jid}:confirm-and-generate", {"idempotency_key": f"conf-vs02-{n}", "items": [
            {"item_id": it["id"], "prompt_revision_id": it["current_prompt"],
             "expected_item_revision": it["revision"]}]})
    gen = [t for t in api.studio.journal.tasks.list(project_id=pid) if t.stage == "generate"]
    assert len(gen) == 3 and a["source"]["asset_id"] != b["source"]["asset_id"]
    assert len({t.residency for t in gen}) == 1  # different sources and rows: one model signature
    assert len({t.logical_key for t in gen}) == 3 and len({t.item_id for t in gen}) == 3
    assert len({t.inputs["prompt_revision_id"] for t in gen}) == 3  # ... but distinct task inputs
    for t in gen:
        for ident in (t.item_id, t.job_id, a["plan_id"], b["plan_id"], a["source"]["asset_id"],
                      b["source"]["asset_id"], a["source"]["version_id"], b["source"]["version_id"]):
            assert ident not in t.residency


# --- VS03: the cpu lane never waits for gpu0 ------------------------------------------------------------------------
class _GatedEngine(FakeEngine):
    """Keeps generation 'running' until released, so a gpu0 pass stays active for as long as the test needs."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()

    def status(self, prompt_id: str) -> Any:
        st = super().status(prompt_id)
        return st if self.gate.is_set() or st.state == "unknown" else type(st)("running")


def test_vs03_direct_transform_runs_on_cpu_while_gpu0_pass_is_active(make_api, tmp_path) -> None:
    engine, aux, w3d = _GatedEngine(), FakeAux(), FakeWorker3d()
    api = make_api(studio=build_studio(make_settings(tmp_path), engine, aux, w3d))
    pid = new_project(api)
    jid = variant_jobs(api, pid, 1, "vs03a")["job_ids"][0]
    api.post(f"{V2}/{pid}/jobs/{jid}:run", {"idempotency_key": "run-vs03-a"})
    api.wait_ops()
    it = api.get(f"{V2}/{pid}/jobs/{jid}")["items"][0]
    api.post(f"{V2}/{pid}/jobs/{jid}:confirm-and-generate", {"idempotency_key": "conf-vs03-a", "items": [
        {"item_id": it["id"], "prompt_revision_id": it["current_prompt"], "expected_item_revision": it["revision"]}]})
    (edit,) = [t for t in api.studio.journal.tasks.list(project_id=pid) if t.stage == "generate"]
    assert wait_until(lambda: api.studio.journal.tasks.get(edit.id).state == "running"  # type: ignore[union-attr]
                      and any(c[0] == "submit_edit" for c in engine.calls), 10)
    calls_before, loads_before, w3d_before = list(aux.calls), dict(aux.loads), list(w3d.calls)
    plain = _png_src(api, pid, "Plain", (10, 100, 200))
    d = _draft(api, pid, plain, "direct_transform", "draft-vs03-b",
               rows=[{"label": "Small", "raster_transform": RESIZE}])
    djid = _create(api, pid, d, "jobs-vs03-b")["job_ids"][0]
    ditem = api.get(f"{V2}/{pid}/jobs/{djid}")["items"][0]
    api.post(f"{V2}/{pid}/jobs/{djid}:run-transform", {"idempotency_key": "xform-vs03-b", "items": [
        {"item_id": ditem["id"], "expected_item_revision": ditem["revision"]}]})

    def built() -> bool:
        b = api.get(f"{V2}/{pid}/jobs/{djid}")["items"][0]["build"]
        return b is not None and b["result"] == "valid"
    assert wait_until(built, 10)
    assert api.studio.journal.tasks.get(edit.id).state == "running"  # type: ignore[union-attr]  # gpu0 still busy
    tasks = api.studio.journal.tasks.list(project_id=pid, job_id=djid)
    assert tasks and {t.lane for t in tasks} == {"cpu"} and {t.residency for t in tasks} == {"cpu"}
    ids = {t.id for t in tasks}
    assert wait_until(lambda: any(set(p["task_ids"]) & ids for p in _passes(api, "cpu")), 5)  # pass closes last
    cpu_pass = next(p for p in _passes(api, "cpu") if set(p["task_ids"]) & ids)
    assert cpu_pass["worker"] is None and cpu_pass["measured"]["model_loads"] is None  # no model involved
    assert aux.calls == calls_before and aux.loads == loads_before and w3d.calls == w3d_before
    engine.gate.set()
    api.wait_ops()
    assert api.studio.journal.tasks.get(edit.id).state == "succeeded"  # type: ignore[union-attr]


# --- VS04: waiting for a human holds no GPU -------------------------------------------------------------------------
def test_vs04_human_gate_waits_hold_no_gpu_work_and_ownership_moves_at_once(make_api, tmp_path) -> None:
    api, engine, aux, w3d = studio_api(make_api, tmp_path)
    pid = new_project(api)
    out = variant_jobs(api, pid, 2, "vs04")
    for n, jid in enumerate(out["job_ids"]):
        api.post(f"{V2}/{pid}/jobs/{jid}:run", {"idempotency_key": f"run-vs04-{n}"})
    api.wait_ops()
    assert all(api.get(f"{V2}/{pid}/jobs/{j}")["items"][0]["current_prompt"] for j in out["job_ids"])
    # prompts wait for confirmation: nothing queued/running/blocked, no pass open, no request holds the device
    assert not api.studio.journal.tasks.list(states=("queued", "running", "reconciling", "blocked"))
    assert not [p for lane in ("gpu0", "gpu1", "cpu") for p in _passes(api, lane) if p["ended_at"] is None]
    assert aux.gpu.active == 0 and w3d.gpu.active == 0 and engine.calls == []
    lane = api.studio.lanes["gpu1"]
    assert lane.public()["state"] == "owned" and lane.owner == "aux"  # weights may stay loaded for reuse ...
    epoch = lane.acquire("worker3d")  # ... but the lease is instantly transferable: aux drains without waiting
    assert lane.owner == "worker3d" and epoch > 0 and aux.gpu.admitting is False and aux.gpu.active == 0
    assert aux.loaded == {"vlm": False, "birefnet": False}  # released, acknowledged with zero active work


# --- VM: migration / coexistence with pre-variant projects -----------------------------------------------------------
def _tree_hashes(root: Path) -> dict[str, str]:
    import hashlib

    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and p.parent.name != "_control"}


def _legacy(api: Api, tmp_path: Path, commit: str) -> tuple[str, Path]:
    root = tmp_path / "projects" / f"legacy-{commit}"
    shutil.copytree(FIXTURES / commit / "project", root)
    return api.post(f"{P}:register", {"root": str(root)})["id"], root


@pytest.mark.parametrize("commit", ["a70232b", "60ff832"])
def test_vm01_pre_variant_project_opens_without_families_and_immutable_bytes_stay(make_api, tmp_path,
                                                                                 commit: str) -> None:
    api: Api = make_api()
    pid, root = _legacy(api, tmp_path, commit)
    before = _tree_hashes(root)
    assets = api.get(f"{P}/{pid}/assets")["items"]
    assert len(assets) >= 3 and all(a["family_id"] is None and a["family_name"] is None for a in assets)
    assert api.get(f"{P}/{pid}/assets?family_id=fam_0000000000000000")["total"] == 0
    # an index written before families existed: reopening adds the columns and rebuilds the memberships
    index = api.studio.registry.get(pid).index
    index._db.execute("ALTER TABLE assets DROP COLUMN family_id")
    index._db.execute("ALTER TABLE assets DROP COLUMN family_name")
    index._db.execute("DELETE FROM assets")
    api.c.__exit__(None, None, None)
    api2: Api = make_api()
    ctx = api2.studio.registry.get(pid)
    cols = {r[1] for r in ctx.index._db.execute("PRAGMA table_info(assets)")}
    assert {"family_id", "family_name"} <= cols and not ctx.index.needs_rebuild()
    again = api2.get(f"{P}/{pid}/assets")["items"]
    assert {a["asset_id"] for a in again} == {a["asset_id"] for a in assets}
    assert all(a["family_id"] is None for a in again)
    assert api2.post(f"{P}/{pid}/storage:rebuild-index")["errors"] == 0
    for a in assets:
        assert api2.get(f"{P}/{pid}/assets/{a['asset_id']}")["family_id"] is None
        assert api2.get(f"{P}/{pid}/assets/{a['asset_id']}/versions")
    after = _tree_hashes(root)
    assert {k: v for k, v in after.items() if k in before} == before  # nothing written earlier is rewritten
    immutable = ("blobs/", "versions/", "artifacts/", "manifests/")
    assert {k for k in after if k.startswith(immutable)} == {k for k in before if k.startswith(immutable)}


def test_vm02_legacy_jobs_new_batches_and_variant_jobs_coexist(make_api, tmp_path) -> None:
    api: Api = make_api()
    pid, _ = _legacy(api, tmp_path, "60ff832")
    legacy_ids = {j["id"] for j in api.get(f"{V2}/{pid}/jobs")["jobs"]}
    assert len(legacy_ids) == 4 and all(i.startswith("bat_") for i in legacy_ids)
    out = variant_jobs(api, pid, 2, "vm02")
    jobs = {j["id"]: j for j in api.get(f"{V2}/{pid}/jobs")["jobs"]}
    assert set(jobs) == legacy_ids | set(out["job_ids"])
    assert all(jobs[i]["legacy"] and jobs[i]["batch"] is None and jobs[i]["source"] != "variant" for i in legacy_ids)
    assert all(jobs[i]["source"] == "variant" and not jobs[i]["legacy"] and jobs[i]["batch"]["id"] == out["batch_id"]
               for i in out["job_ids"])
    batches = api.get(f"{V2}/{pid}/batches")["batches"]
    assert [b["id"] for b in batches] == [out["batch_id"]]  # legacy records are Jobs, never grouping Batches
    assert batches[0]["job_ids"] == out["job_ids"]
    # a user Batch may group a legacy Job with a variant Job; both stay readable through v2 and v1
    mixed = api.post(f"{V2}/{pid}/batches", {"title": "Mixed", "job_ids": [sorted(legacy_ids)[0], out["job_ids"][0]],
                                             "idempotency_key": "batch-vm02-mixed"})["batch"]
    assert mixed["jobs"] == 2 and api.get(f"{V2}/{pid}/batches/{mixed['id']}")["job_ids"] == mixed["job_ids"]
    for jid in (*legacy_ids, *out["job_ids"]):
        d = api.get(f"{V2}/{pid}/jobs/{jid}")
        assert d["items"] and all(i["job_id"] == jid for i in d["items"])
        assert api.get(f"{P}/{pid}/batches/{jid}")["items"]
    assert {b["id"] for b in api.get(f"{P}/{pid}/batches")["batches"]} == legacy_ids | set(out["job_ids"])


def test_vm03_legacy_recipe_snapshot_still_refuses_builds_with_a_variant_present(make_api, tmp_path) -> None:
    api: Api = make_api()
    pid, _ = _legacy(api, tmp_path, "a70232b")
    out = variant_jobs(api, pid, 1, "vm03")
    assert api.get(f"{V2}/{pid}/jobs/{out['job_ids'][0]}")["source"] == "variant"
    job = next(j for j in api.get(f"{P}/{pid}/batches")["batches"] if j["kind"] == "model3d")
    it = next(i for i in api.get(f"{P}/{pid}/batches/{job['id']}")["items"] if i["approval"])
    for route in (f"{P}/{pid}/batches/{job['id']}:build-approved", f"{V2}/{pid}/jobs/{job['id']}:build-approved"):
        r = api.raw("POST", route, json={"idempotency_key": f"vm03-{route[-24:]}", "items": [
            {"item_id": it["id"], "approval_id": it["approval"], "expected_item_revision": it["revision"]}]})
        assert r.status_code in (200, 202), r.text
        res = r.json()["results"][0]
        assert res["ok"] is False and res["code"] == "legacy_recipe" and "a70232b" in res["message"]
        assert r.json()["operation"] is None
    assert not [t for t in api.studio.journal.tasks.list(project_id=pid) if t.stage in ("segment", "sample", "bake")]
    legacy = next(j for j in api.get(f"{V2}/{pid}/jobs")["jobs"] if j["id"] == job["id"])
    assert legacy["legacy_recipe"] and legacy["source"] != "variant"
