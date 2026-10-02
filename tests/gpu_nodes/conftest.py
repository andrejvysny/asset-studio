"""Node-mode real-GPU acceptance (make acceptance-gpu-nodes). Operator-run on the 2x4090 box; never runs in CI.

Talks HTTP to a running Studio in node mode (STUDIO_URL, no default: an unset URL skips the suite) and reads the
runner side ONLY through Studio's operator API (/api/v1/runners, /api/v1/runtime, /api/v2/tasks). It never inspects
ComfyUI or /models directly: in node mode Studio owns neither (the direct-mode suite in tests/gpu cannot certify that).
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time
import uuid
from typing import Any

import httpx
import pytest

URL = os.environ.get("STUDIO_URL", "")
H = {"x-assetstudio": "1"}
CONCEPT_OPS = ("aux.enhance", "image.t2i", "aux.qa")
MODEL3D_OPS = CONCEPT_OPS + ("aux.cutout", "worker3d.generate", "worker3d.export")
FAST = {"speed_preset": "lightning_8step", "width": 1024, "height": 1024}


def c() -> httpx.Client:
    return httpx.Client(base_url=URL, timeout=60, headers=H)


def wait(pred: Any, timeout: float, what: str, poll: float = 3.0) -> Any:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            last = pred()
        except httpx.HTTPError:
            last = None
        if last:
            return last
        time.sleep(poll)
    raise AssertionError(f"timeout waiting for {what}")


def ops_idle(cl: httpx.Client, pid: str) -> bool:
    ops = cl.get("/api/v1/operations", params={"project_id": pid, "active": True}).json()["operations"]
    blocked = [o for o in ops if o["state"] == "blocked"]
    assert not blocked, f"blocked operations: {blocked}"
    return not ops


def detail(cl: httpx.Client, pid: str, bid: str) -> dict:
    return cl.get(f"/api/v1/projects/{pid}/batches/{bid}").json()


def runners(cl: httpx.Client) -> list[dict]:
    return [r for r in cl.get("/api/v1/runners").json()["runners"] if r["state"] == "active"]


def runner_detail(cl: httpx.Client, runner_id: str) -> dict:
    return cl.get(f"/api/v1/runners/{runner_id}").json()


def all_attempts(cl: httpx.Client) -> list[dict]:
    """Every attempt of every active runner (the operator API lists a runner's attempts, newest first)."""
    return [a for r in runners(cl) for a in runner_detail(cl, r["id"])["attempts"]]


def attempts_of(cl: httpx.Client, task_ids: set[str]) -> list[dict]:
    return [a for a in all_attempts(cl) if a["task_id"] in task_ids]


def project_task_ids(cl: httpx.Client, pid: str) -> set[str]:
    return {t["id"] for t in cl.get("/api/v2/tasks", params={"project_id": pid}).json()["tasks"]}


def drained(cl: httpx.Client) -> bool:
    """Every attempt has a Studio disposition (its receipt can be delivered), so the runner spool empties."""
    rows = all_attempts(cl)
    return bool(rows) and all(a["disposition"] is not None for a in rows)


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    base = shlex.split(os.environ.get("NODES_COMPOSE", "docker compose -f compose.gpu-local.yml -f compose.nodes.yml"))
    return subprocess.run([*base, *args], check=check, capture_output=True, text=True, timeout=600)


def runner_service(active: list[dict], runner_id: str, mapping: str = "") -> str:
    """Compose service of the runner that owns an attempt. `mapping` ("runner name=service,...", env
    GPU_NODES_RUNNER_SERVICES) resolves several runners on one stack; a lone runner is the compose `runner` service.
    Refuses to guess: killing a runner that does not own the attempt would make a chaos test pass vacuously."""
    owner = next((r for r in active if r["id"] == runner_id), None)
    assert owner is not None, f"runner {runner_id} owning the attempt is not active: {[r['id'] for r in active]}"
    services = dict(p.split("=", 1) for p in mapping.split(",") if "=" in p)
    if owner["name"] in services:
        return services[owner["name"]]
    assert len(active) == 1, (f"{len(active)} runners are active and {owner['name']!r} is not in "
                              f"GPU_NODES_RUNNER_SERVICES (name=service,...)")
    return "runner"


def require_ready(cl: httpx.Client, ops: tuple[str, ...], recipe_id: str | None = None) -> dict:
    """Fail fast (never skip: the operator asked for this suite) with the runner_readiness reasons."""
    rt = cl.get("/api/v1/runtime").json()
    fresh = [r for r in runners(cl) if (r["session"] or {}).get("fresh")]
    assert fresh, "no runner has a fresh session; start the runner (make up-nodes) and check `assetstudio runners list`"
    by_op = {r["operation"]: r for r in rt["runner_readiness"]}
    bad = {op: by_op.get(op, {"reasons": ["operation unknown to Studio"]})["reasons"] for op in ops
           if not by_op.get(op, {}).get("ready")}
    assert not bad, f"operations not ready (runner_readiness reasons): {bad}"
    if recipe_id:
        recipe = next(r for r in rt["recipes"] if r["id"] == recipe_id)
        assert recipe["generation"]["state"] == "ready", recipe["generation"]
        assert recipe["build"]["state"] in ("ready", "experimental"), recipe["build"]
    assert rt["simulated"] is False, "acceptance must not run against simulated runners"
    return rt


@pytest.fixture(scope="session")
def stack() -> dict:
    if not URL:
        pytest.skip("STUDIO_URL not set (make acceptance-gpu-nodes sets it)")
    try:
        with c() as cl:
            rt = cl.get("/api/v1/runtime").json()
    except httpx.HTTPError as e:
        pytest.skip(f"Studio unreachable at {URL}: {e}")
    if rt.get("engine_mode") != "nodes":
        pytest.skip(f"Studio at {URL} is not in node mode (engine_mode={rt.get('engine_mode')!r})")
    return rt


def make_project(cl: httpx.Client, kind: str) -> str:
    pid = cl.post("/api/v1/projects", json={"name": f"gpu-nodes-{kind}-{uuid.uuid4().hex[:6]}"}).json()["id"]
    cfg = cl.get(f"/api/v1/projects/{pid}/config").json()["config"]
    if kind == "model3d":
        cfg["categories"] = [{"id": "props", "slug": "props", "label": "Props", "defaults": {
            "kind": "model3d", "budget": {"triangles": {"min": 2000, "max": 40000}}}}]
        cfg["pipelines"] = {"model3d.default": {"parameters": {**FAST, "candidate_count": 2}}}
    else:
        cfg["categories"] = [{"id": "concept", "slug": "concept", "label": "Concept",
                              "defaults": {"kind": "concept_art", "naming": "concept_{name}"}}]
        cfg["pipelines"] = {"concept.default": {"parameters": FAST}}
    r = cl.patch(f"/api/v1/projects/{pid}/config", json={"expected_revision": 1, "config": cfg})
    assert r.status_code == 200, r.text
    return pid


def create_batch(cl: httpx.Client, pid: str, category: str, items: list[tuple[str, str]]) -> str:
    out = cl.post(f"/api/v1/projects/{pid}/batches", json={
        "title": "GPU nodes acceptance", "category_id": category, "idempotency_key": str(uuid.uuid4()),
        "items": [{"name": n, "brief": b} for n, b in items]}).json()
    bid = out["batch"]["id"]
    wait(lambda: ops_idle(cl, pid), 900, "enhancement")
    d = detail(cl, pid, bid)
    assert all(i["prompt"] and i["prompt"]["origin"] == "enhanced" for i in d["items"]), "enhancement failed"
    return bid


def confirm(cl: httpx.Client, pid: str, bid: str) -> None:
    d = detail(cl, pid, bid)
    conf = cl.post(f"/api/v1/projects/{pid}/batches/{bid}:confirm-and-generate", json={
        "idempotency_key": str(uuid.uuid4()), "items": [{"item_id": i["id"], "prompt_revision_id": i["current_prompt"],
                                                       "expected_item_revision": i["revision"]} for i in d["items"]]}
        ).json()
    assert all(x["ok"] for x in conf["results"]), conf


def approve_best(cl: httpx.Client, pid: str, bid: str) -> list[dict]:
    d = detail(cl, pid, bid)
    approvals = []
    for it in d["items"]:
        cands = it["candidate_set"]["candidates"]
        cand = next((x for x in cands if x["qa"] and x["qa"]["status"] == "recommended"), cands[0])
        approvals.append({"item_id": it["id"], "expected_item_revision": it["revision"],
                          "candidate_set_id": it["candidate_set"]["id"], "candidate_id": cand["id"],
                          "image_sha256": cand["sha256"], "prompt_revision_id": it["candidate_set"]["prompt_revision_id"],
                          "qa_evaluation_id": cand["qa"]["id"] if cand["qa"] else None,
                          "override_qa": not (cand["qa"] and cand["qa"]["status"] == "recommended")})
    res = cl.post(f"/api/v1/projects/{pid}/batches/{bid}:approve-candidates",
                  json={"idempotency_key": str(uuid.uuid4()), "items": approvals}).json()
    assert all(x["ok"] for x in res["results"]), res
    return detail(cl, pid, bid)["items"]


def build_accept_publish(cl: httpx.Client, pid: str, bid: str, build_timeout: float = 2400) -> list[dict]:
    items = detail(cl, pid, bid)["items"]
    cl.post(f"/api/v1/projects/{pid}/batches/{bid}:build-approved", json={
        "idempotency_key": str(uuid.uuid4()), "items": [{"item_id": i["id"], "approval_id": i["approval"],
                                                       "expected_item_revision": i["revision"]} for i in items]})
    wait(lambda: ops_idle(cl, pid), build_timeout, "build")
    items = detail(cl, pid, bid)["items"]
    assert all(i["build"] and i["build"]["result"] == "valid" for i in items), [i["tasks"] for i in items]
    cl.post(f"/api/v1/projects/{pid}/batches/{bid}:accept-builds", json={
        "idempotency_key": str(uuid.uuid4()), "items": [{"item_id": i["id"], "build_run_id": i["current_build"],
                                                       "expected_item_revision": i["revision"]} for i in items]})
    prev = cl.get(f"/api/v1/projects/{pid}/batches/{bid}/publish-preview").json()["items"]
    cl.post(f"/api/v1/projects/{pid}/batches/{bid}:publish", json={"idempotency_key": str(uuid.uuid4()), "items": [
        {"item_id": p["item_id"], "build_run_id": p["build_run_id"], "expected_item_revision": p["expected_item_revision"]}
        for p in prev]})
    wait(lambda: ops_idle(cl, pid), 300, "publication")
    return detail(cl, pid, bid)["items"]
