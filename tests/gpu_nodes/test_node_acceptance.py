"""STRICT node-mode acceptance on real hardware (spec A01, A02, A04, A07, A08; make acceptance-gpu-nodes).

Real Qwen-Image + Qwen3-VL + BiRefNet + TRELLIS.2 behind a runner; Studio has no /models and no GPU.
Env: STUDIO_URL (set by the make target), GPU_NODES_DOCKER=1 (A01 container check), GPU_NODES_CHAOS=1 (kill/restart
scenarios), GPU_NODES_RUNNER_B=1 (a second runner is registered), NODES_COMPOSE (compose file flags).
"""
from __future__ import annotations

import os

import pytest

from tests.gpu_nodes.conftest import (
    CONCEPT_OPS,
    MODEL3D_OPS,
    approve_best,
    attempts_of,
    build_accept_publish,
    c,
    compose,
    confirm,
    create_batch,
    detail,
    drained,
    make_project,
    ops_idle,
    project_task_ids,
    require_ready,
    runner_detail,
    runners,
    wait,
)

pytestmark = [pytest.mark.gpu_nodes, pytest.mark.usefixtures("stack")]
CHAOS = pytest.mark.skipif(os.environ.get("GPU_NODES_CHAOS") != "1", reason="set GPU_NODES_CHAOS=1 (kills containers)")
ITEMS = [("Tavern interior", "warm tavern interior with long wooden tables"),
         ("Harbour at dusk", "small fishing harbour at dusk")]
CRATE = [("Wooden supply crate", "small wooden supply crate with iron corner brackets")]


def _committed(rows: list[dict]) -> None:
    assert rows and all(a["state"] == "committed" and a["disposition"] == "committed" for a in rows), \
        [(a["operation"], a["state"], a["disposition"]) for a in rows]
    assert all(a["runner_id"] for a in rows)


def test_a01_studio_has_no_models_and_models_are_runner_verified() -> None:
    with c() as cl:
        rt = require_ready(cl, CONCEPT_OPS)
        assert rt["engine_mode"] == "nodes"
        ready = [m for m in rt["models"] if m["ready"]]
        assert ready, "no model is ready"
        assert all("runner" in m["detail"] for m in ready), [(m["key"], m["detail"]) for m in ready]
        assert rt["gpus"] and all(g["source"].startswith("runner ") for g in rt["gpus"]), rt["gpus"]
    if os.environ.get("GPU_NODES_DOCKER") != "1":
        pytest.skip("set GPU_NODES_DOCKER=1 to check the Studio container filesystem")
    assert compose("exec", "-T", "studio", "test", "!", "-e", "/models", check=False).returncode == 0, \
        "Studio container must not have /models in node mode"


def test_a02_concept_art_lifecycle_through_the_runner() -> None:
    with c() as cl:
        require_ready(cl, CONCEPT_OPS, "concept.default")
        pid = make_project(cl, "concept")
        bid = create_batch(cl, pid, "concept", ITEMS)
        confirm(cl, pid, bid)
        wait(lambda: ops_idle(cl, pid), 3600, "generation + QA")
        d = detail(cl, pid, bid)
        assert all(len(i["candidate_set"]["candidates"]) == 4 for i in d["items"])
        assert len({cd["seed"] for i in d["items"] for cd in i["candidate_set"]["candidates"]}) == 8
        approve_best(cl, pid, bid)
        build_accept_publish(cl, pid, bid)
        lib = cl.get(f"/api/v1/projects/{pid}/assets").json()
        assert lib["total"] == 2, "strict: 2 published versions"
        wait(lambda: drained(cl), 120, "every attempt has a disposition", poll=2)
        rows = attempts_of(cl, project_task_ids(cl, pid))
        _committed(rows)
        assert {"aux.enhance", "image.t2i", "aux.qa"} <= {a["operation"] for a in rows}
        assert sum(a["operation"] == "image.t2i" for a in rows) == 8  # 2 items x 4 candidates, one execution each
        assert max(a["generation"] for a in rows) == 1
        # spool drained: no attempt of any runner is left without a delivered disposition
        for r in runners(cl):
            assert all(a["disposition"] for a in runner_detail(cl, r["id"])["attempts"])


def test_a02_model3d_lifecycle_on_the_aux3d_slot() -> None:
    with c() as cl:
        require_ready(cl, MODEL3D_OPS, "model3d.default")
        pid = make_project(cl, "model3d")
        bid = create_batch(cl, pid, "props", CRATE)
        confirm(cl, pid, bid)
        wait(lambda: ops_idle(cl, pid), 1800, "generation + QA")
        approve_best(cl, pid, bid)
        items = build_accept_publish(cl, pid, bid)
        b = items[0]["build"]
        assert b["artifacts"].keys() >= {"model", "raw", "cutout", "preview", "meta"}
        checks = {x["id"]: x["ok"] for x in b["validation"]["checks"]}
        assert checks and all(checks.values()), checks  # GLB structurally valid
        content = cl.get(f"/api/v1/projects/{pid}/artifacts/{b['artifacts']['model']}/content").content
        assert content[:4] == b"glTF", "published model is not a GLB"
        assert cl.get(f"/api/v1/projects/{pid}/assets").json()["total"] == 1
        wait(lambda: drained(cl), 120, "every attempt has a disposition", poll=2)
        rows = attempts_of(cl, project_task_ids(cl, pid))
        _committed(rows)
        w3d = sorted(a["operation"] for a in rows if a["operation"].startswith("worker3d."))
        assert w3d == ["worker3d.export", "worker3d.generate"], w3d  # segment/sample/bake: one execution each
        by_id = {r["id"]: r for r in runners(cl)}
        for a in rows:
            if a["operation"].startswith(("worker3d.", "aux.cutout")):
                slots = {s["slot_id"]: s["capability"] for s in by_id[a["runner_id"]]["slots"]}
                assert "aux3d" in slots.values(), slots


@CHAOS
def test_a08_runner_killed_mid_sample_reconciles_without_second_trellis_run() -> None:
    with c() as cl:
        require_ready(cl, MODEL3D_OPS, "model3d.default")
        pid = make_project(cl, "model3d")
        bid = create_batch(cl, pid, "props", CRATE)
        confirm(cl, pid, bid)
        wait(lambda: ops_idle(cl, pid), 1800, "generation + QA")
        approve_best(cl, pid, bid)
        items = detail(cl, pid, bid)["items"]
        cl.post(f"/api/v1/projects/{pid}/batches/{bid}:build-approved", json={"idempotency_key": "chaos-sample", "items": [
            {"item_id": i["id"], "approval_id": i["approval"], "expected_item_revision": i["revision"]} for i in items]})
        tasks = project_task_ids(cl, pid)
        wait(lambda: any(a["operation"] == "worker3d.generate" and a["state"] in ("leased", "executing")
                         for a in attempts_of(cl, tasks)), 600, "sampling attempt", poll=1)
        compose("kill", "runner")
        compose("start", "runner")
        wait(lambda: ops_idle(cl, pid), 3600, "reconciled build", poll=5)
        assert detail(cl, pid, bid)["items"][0]["build"]["result"] == "valid"
        rows = [a for a in attempts_of(cl, tasks) if a["operation"] == "worker3d.generate"]
        assert len(rows) == 1 and rows[0]["generation"] == 1 and rows[0]["state"] == "committed", rows
        build_accept_publish(cl, pid, bid)
        assert cl.get(f"/api/v1/projects/{pid}/assets").json()["total"] == 1, "exactly one publication"


@CHAOS
def test_a07_studio_restart_mid_generation_does_not_duplicate_executions() -> None:
    with c() as cl:
        require_ready(cl, CONCEPT_OPS, "concept.default")
        pid = make_project(cl, "concept")
        bid = create_batch(cl, pid, "concept", ITEMS)
        confirm(cl, pid, bid)
        tasks = project_task_ids(cl, pid)
        wait(lambda: sum(a["operation"] == "image.t2i" for a in attempts_of(cl, tasks)) >= 1, 900, "first t2i", poll=2)
        compose("restart", "studio")
        wait(lambda: cl.get("/api/health").status_code == 200, 180, "studio restart", poll=2)
        wait(lambda: ops_idle(cl, pid), 3600, "generation + QA", poll=5)
        d = detail(cl, pid, bid)
        assert all(len(i["candidate_set"]["candidates"]) == 4 for i in d["items"])
        rows = [a for a in attempts_of(cl, project_task_ids(cl, pid)) if a["operation"] == "image.t2i"]
        assert len(rows) == 8, f"expected 8 ComfyUI executions (2 items x 4), found {len(rows)}"
        assert {a["generation"] for a in rows} == {1}, "an attempt was re-placed (generation 2)"


@pytest.mark.skipif(os.environ.get("GPU_NODES_RUNNER_B") != "1", reason="set GPU_NODES_RUNNER_B=1 (node B registered)")
def test_a02_a04_two_runner_placement() -> None:
    with c() as cl:
        rs = [r for r in runners(cl) if (r["session"] or {}).get("fresh")]
        assert len(rs) >= 2, f"need 2 fresh runners, found {[r['name'] for r in rs]}"
        uuids = [d["uuid"] for r in rs for d in r["devices"]]
        assert len(uuids) == len(set(uuids)), f"device UUIDs collide across runners: {uuids}"  # A02: even if index 0
        advertised: dict[str, set[str]] = {}
        for r in rs:
            for s in r["slots"]:
                for e in s["engines"]:
                    for op in e["operations"]:
                        advertised.setdefault(op, set()).add(r["id"])
        require_ready(cl, CONCEPT_OPS, "concept.default")
        pid = make_project(cl, "concept")
        bid = create_batch(cl, pid, "concept", ITEMS[:1])
        confirm(cl, pid, bid)
        wait(lambda: ops_idle(cl, pid), 3600, "generation + QA")
        wait(lambda: drained(cl), 120, "dispositions", poll=2)
        rows = attempts_of(cl, project_task_ids(cl, pid))
        _committed(rows)
        for a in rows:  # A04: an operation only one runner can serve (its model receipt) lands on that runner
            assert a["runner_id"] in advertised[a["operation"]], (a["operation"], a["runner_id"])
        exclusive = {op: next(iter(ids)) for op, ids in advertised.items() if len(ids) == 1}
        for a in rows:
            if a["operation"] in exclusive:
                assert a["runner_id"] == exclusive[a["operation"]]
        assert exclusive, "no operation is exclusive to one runner: seed a model receipt on node B only (A04)"
