"""Variants Phase E-c contracts: explicit async source analysis + row suggestion for DRAFTS (simulated aux)."""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from assetstudio_server.services.variant_analysis import normalise_label, normalise_rows

from tests.conftest import Api, new_project
from tests.contract.test_api_variants import V2, P, _draft, _get, _png_src, _prepare


def _ready_draft(api: Api, key: str = "plan-draft-1", **kw: Any) -> tuple[str, dict]:
    pid = new_project(api)
    d = _draft(api, pid, _png_src(api, pid), "image_edit", key, **kw)
    return pid, _prepare(api, pid, d)


def _url(pid: str, d: dict, action: str) -> str:
    return f"{P}/{pid}/variant-drafts/{d['id']}:{action}"


def _analyze(api: Api, pid: str, d: dict, key: str = "analyze-key-1", status: Any = (200, 202)) -> Any:
    return api.post(_url(pid, d, "analyze-source"), {"idempotency_key": key}, status=status)


def _suggest(api: Api, pid: str, d: dict, count: int, key: str, status: Any = 202, **kw: Any) -> Any:
    return api.post(_url(pid, d, "suggest-plan"), {"count": count, "idempotency_key": key, **kw}, status=status)


def _wait_state(api: Api, pid: str, d: dict, kind: str, states: tuple[str, ...]) -> dict:
    end = time.monotonic() + 15
    cur: dict = {}
    while time.monotonic() < end:
        cur = _get(api, pid, d["id"])
        if cur["tasks"][kind]["state"] in states:
            return cur
        time.sleep(0.05)
    raise AssertionError(cur["tasks"])


def test_analyze_queues_one_gpu1_task_stores_analysis_and_caches(make_api) -> None:
    api = make_api()
    pid, d = _ready_draft(api)
    aux = api.studio.aux
    out = _analyze(api, pid, d, status=202)
    task = api.studio.journal.tasks.get(out["task_id"])
    assert task is not None and (task.lane, task.stage, task.family) == ("gpu1", "variant_analyze", "plan")
    assert task.job_id == task.item_id == d["id"] and task.residency.startswith("aux.vlm:")
    cur = _wait_state(api, pid, d, "analyze", ("succeeded",))
    assert aux.calls.count("analyze_source") == 1
    assert cur["analysis_id"].startswith("vsa_") and cur["suggestion"] is None
    rec = api.get(f"{P}/{pid}/variant-drafts/{d['id']}/analysis")
    assert rec["id"] == cur["analysis_id"] and rec["draft_id"] == d["id"] and rec["observations"]
    assert rec["reference_set_id"] == d["reference_set_id"] and rec["input_hashes"]
    again = _analyze(api, pid, d, key="analyze-key-2", status=200)
    assert again["task_id"] is None and again["analysis"]["id"] == rec["id"]
    api.wait_ops()
    assert aux.calls.count("analyze_source") == 1 and len(api.studio.journal.tasks.list(project_id=pid)) == 1


def test_analyze_sends_actual_reference_bytes(make_api) -> None:
    api = make_api()
    pid, d = _ready_draft(api)
    seen: list[list[tuple[bytes, str]]] = []
    aux = api.studio.aux
    orig = aux.analyze_source

    def spy(*, images: list[tuple[bytes, str]], **kw: Any) -> Any:
        seen.append(images)
        return orig(images=images, **kw)
    aux.analyze_source = spy
    _analyze(api, pid, d, status=202)
    _wait_state(api, pid, d, "analyze", ("succeeded",))
    (images,) = seen
    ctx = api.studio.registry.get(pid)
    assert len(images) == 1 and images[0][1] == "image" and len(images[0][0]) > 100
    ref = ctx.store.repo.read_object(f"source-references/{d['reference_set_id']}.json")
    sha = json.loads(ref.data)["images"][0]["sha256"]
    assert hashlib.sha256(images[0][0]).hexdigest() == sha


def test_suggest_normalises_labels_keeps_rows_and_apply_is_explicit(make_api) -> None:
    api = make_api()
    pid, d = _ready_draft(api)
    out = _suggest(api, pid, d, 6, "suggest-key-1")
    cur = _wait_state(api, pid, d, "suggest", ("succeeded",))
    sug = cur["suggestion"]
    labels = [r["label"] for r in sug["rows"]]
    assert sug["task_id"] == out["task_id"] and len(labels) == 6 and len(set(labels)) == 6
    assert all(x == x[:1].upper() + x[1:] and "_" not in x for x in labels) and sug["count"] == 6
    assert cur["rows"] == d["rows"]  # never overwritten by a suggestion
    assert api.studio.aux.calls.count("suggest_variants") == 1
    patched = api.raw("PATCH", f"{P}/{pid}/variant-drafts/{d['id']}", json={"expected_revision": cur["revision"],
                                                                             "rows": []}).json()
    applied = api.post(_url(pid, d, "apply-suggestion"),
                       {"expected_revision": patched["revision"], "mode": "replace_empty"})
    assert [r["label"] for r in applied["rows"]] == labels and len({r["id"] for r in applied["rows"]}) == 6
    assert all(r["candidate_count"] == applied["candidates_per_row"] for r in applied["rows"])
    r = api.raw("POST", _url(pid, d, "apply-suggestion"),
                json={"expected_revision": applied["revision"], "mode": "replace_empty"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "rows_not_empty"
    more = api.post(_url(pid, d, "apply-suggestion"),
                    {"expected_revision": applied["revision"], "mode": "append", "indices": [0, 2]})
    assert len(more["rows"]) == 8 and more["rows"][:6] == applied["rows"]
    bad = api.raw("POST", _url(pid, d, "apply-suggestion"),
                  json={"expected_revision": more["revision"], "mode": "append", "indices": [9]})
    assert bad.status_code == 422


def test_label_normalisation() -> None:
    assert normalise_label("compact_pine") == "Compact pine"
    assert normalise_label("  tall-thin__tree ") == "Tall thin tree"
    assert len(normalise_label("x" * 100)) == 60
    rows = normalise_rows([{"label": "compact_pine", "change_request": "a"}, {"label": "Compact pine"},
                           {"label": "compact-PINE"}, {"label": "wide_oak", "change_request": " b "}, {"bad": 1}], 5)
    assert rows == [{"label": "Compact pine", "change_request": "a"}, {"label": "Wide oak", "change_request": "b"}]


def test_without_references_task_fails_visibly(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    d = _draft(api, pid, _png_src(api, pid), "image_edit", "plan-draft-2")
    assert d["reference_set_id"] is None
    _analyze(api, pid, d, status=202)
    cur = _wait_state(api, pid, d, "analyze", ("failed",))
    assert cur["tasks"]["analyze"]["code"] == "references_missing" and cur["tasks"]["suggest"]["state"] == "idle"
    assert [c for c in api.studio.aux.calls if c not in ("lease", "unload")] == []  # GPU handoff only, no inference


def test_aux_unavailable_blocks_only_the_planning_task(make_api) -> None:
    api = make_api()
    pid, d = _ready_draft(api)
    api.studio.aux = None
    _suggest(api, pid, d, 3, "suggest-key-2")
    cur = _wait_state(api, pid, d, "suggest", ("blocked",))
    assert cur["tasks"]["suggest"]["code"] == "aux_unconfigured" and cur["suggestion"] is None
    assert api.get(f"{P}/{pid}/variant-drafts/{d['id']}")["id"] == d["id"]
    assert api.get(f"{V2}/{pid}/jobs")["jobs"] == []
    r = api.raw("POST", _url(pid, d, "suggest-plan"), json={"count": 2, "idempotency_key": "suggest-key-3"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "plan_task_active"


def test_draft_tasks_are_invisible_to_jobs_and_batches_and_cancellable(make_api) -> None:
    api = make_api(coordinator=False)
    pid, d = _ready_draft(api)
    out = _suggest(api, pid, d, 2, "suggest-key-4")
    assert _get(api, pid, d["id"])["tasks"]["suggest"]["state"] == "queued"
    assert api.get(f"{V2}/{pid}/jobs")["jobs"] == [] and api.get(f"{V2}/{pid}/batches")["batches"] == []
    assert api.get(f"{V2}/{pid}/runs")["runs"] == []
    t = api.post(f"/api/v2/tasks/{out['task_id']}:cancel")
    assert t["state"] == "cancelled"
    assert _get(api, pid, d["id"])["tasks"]["suggest"]["state"] == "cancelled"
    assert api.get(f"/api/v1/projects/{pid}/summary")["counts"]["jobs"] == 0


def test_passes_do_not_list_drafts_as_jobs(make_api) -> None:
    api = make_api()
    pid, d = _ready_draft(api)
    _suggest(api, pid, d, 2, "suggest-key-5")
    _wait_state(api, pid, d, "suggest", ("succeeded",))
    passes = [p for p in api.get("/api/v2/passes")["passes"] if p["task_ids"]]
    assert passes and all(p["jobs"] == [] for p in passes)
