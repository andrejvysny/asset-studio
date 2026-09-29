"""STRICT real-stack acceptance (make acceptance-gpu): real Qwen3-VL + Qwen-Image + BiRefNet, no simulation.

Success requires valid builds AND published versions; a failed stage is a failure, never an accepted outcome.
Env: STUDIO_URL (default http://127.0.0.1:8190), COMFY_URL (default http://127.0.0.1:8188),
     RESTART_CMD (default `podman restart assetstudio_studio_1`), GPU_QUALITY=1 to also run the 50-step path.
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

pytestmark = pytest.mark.gpu
URL = os.environ.get("STUDIO_URL", "http://127.0.0.1:8190")
COMFY = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
H = {"x-assetstudio": "1"}
ITEMS = [("Tavern interior", "warm tavern interior with long wooden tables"),
         ("Harbour at dusk", "small fishing harbour at dusk"),
         ("Forge", "blacksmith forge with glowing coals"),
         ("Market square", "busy market square with stalls"),
         ("Lighthouse", "stone lighthouse on a cliff"),
         ("Library hall", "tall library hall with ladders"),
         ("Greenhouse", "glass greenhouse full of plants")]


def c() -> httpx.Client:
    return httpx.Client(base_url=URL, timeout=60, headers=H)


def wait(cl: httpx.Client, pred: Any, timeout: float, what: str, poll: float = 3.0) -> Any:
    end = time.monotonic() + timeout
    last = None
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


@pytest.fixture(scope="module")
def project() -> str:
    with c() as cl:
        assert cl.get("/api/health").json()["simulated"] is False, "acceptance must not run on the simulated engine"
        pid = cl.post("/api/v1/projects", json={"name": f"gpu-acceptance-{uuid.uuid4().hex[:6]}"}).json()["id"]
        cfg = cl.get(f"/api/v1/projects/{pid}/config").json()["config"]
        cfg["categories"] = [{"id": "concept", "slug": "concept", "label": "Concept",
                              "defaults": {"kind": "concept_art", "naming": "concept_{name}"}}]
        cfg["pipelines"] = {"concept.default": {"parameters": {"speed_preset": "lightning_8step", "width": 1024,
                                                               "height": 1024}}}
        r = cl.patch(f"/api/v1/projects/{pid}/config", json={"expected_revision": 1, "config": cfg})
        assert r.status_code == 200, r.text
        return pid


def test_seven_item_batch_real_stack(project: str) -> None:
    pid = project
    with c() as cl:
        out = cl.post(f"/api/v1/projects/{pid}/batches", json={
            "title": "GPU acceptance", "category_id": "concept", "idempotency_key": str(uuid.uuid4()),
            "items": [{"name": n, "brief": b} for n, b in ITEMS]}).json()
        bid = out["batch"]["id"]
        wait(cl, lambda: ops_idle(cl, pid), 900, "enhancement")
        d = detail(cl, pid, bid)
        assert all(i["prompt"] and i["prompt"]["origin"] == "enhanced" for i in d["items"]), "enhancement failed"
        assert not any(i["prompt"]["enhancer"].get("simulated") for i in d["items"])
        first = d["items"][0]
        r = cl.post(f"/api/v1/projects/{pid}/batches/{bid}:edit-prompts", json={"items": [{
            "item_id": first["id"], "expected_item_revision": first["revision"],
            "description": first["prompt"]["description"] + ", candle light"}]}).json()
        assert r["results"][0]["ok"], r
        d = detail(cl, pid, bid)
        conf = cl.post(f"/api/v1/projects/{pid}/batches/{bid}:confirm-and-generate", json={
            "idempotency_key": str(uuid.uuid4()), "items": [{"item_id": i["id"], "prompt_revision_id": i["current_prompt"],
                                                           "expected_item_revision": i["revision"]} for i in d["items"]]}).json()
        assert all(x["ok"] for x in conf["results"]), conf
        # R03/R04: restart the Studio mid-generation; the pass must resume without duplicate submissions.
        wait(cl, lambda: any((i["tasks"].get("generate") or {}).get("progress", {}).get("done", 0) >= 2
                             for i in detail(cl, pid, bid)["items"]), 900, "first candidates")
        subprocess.run(shlex.split(os.environ.get("RESTART_CMD", "podman restart assetstudio_studio_1")), check=True)
        wait(cl, lambda: cl.get("/api/health").status_code == 200, 120, "studio restart", poll=2)
        wait(cl, lambda: ops_idle(cl, pid), 3600, "generation + QA")
        d = detail(cl, pid, bid)
        sets = [i["candidate_set"] for i in d["items"]]
        assert all(s and len(s["candidates"]) == 4 for s in sets), "every item needs 4 real candidates"
        seeds = [cd["seed"] for s in sets for cd in s["candidates"]]
        assert len(set(seeds)) == 28
        prompt_ids = [cd["artifact_id"] for s in sets for cd in s["candidates"]]
        assert len(set(prompt_ids)) == 28
        results = [r for s in sets for cd in s["candidates"] for r in (cd["qa"] or {}).get("results", [])]
        assert results and any(r["result"] in ("pass", "fail") for r in results if r["source"] == "vlm"), \
            "real VLM QA must produce answers"
        # no duplicate engine submissions despite the restart: each deterministic prompt id ran at most once
        history = httpx.get(f"{COMFY}/history", params={"max_items": 2000}, timeout=30).json()
        ours = [h for h in history.values() if any("assetstudio/" + bid in str(v) for v in
                                                    (h.get("prompt") or [None, None, {}])[2].values())]
        assert len(ours) == 28, f"expected 28 engine executions, found {len(ours)}"

        # partial decisions: approve 4 (recommended or explicit override), mark 1 regenerate, leave 2 undecided
        items = d["items"]
        approvals = []
        for it in items[:4]:
            cand = next((x for x in it["candidate_set"]["candidates"] if x["qa"] and x["qa"]["status"] == "recommended"),
                        it["candidate_set"]["candidates"][0])
            approvals.append({"item_id": it["id"], "expected_item_revision": it["revision"],
                              "candidate_set_id": it["candidate_set"]["id"], "candidate_id": cand["id"],
                              "image_sha256": cand["sha256"], "prompt_revision_id": it["candidate_set"]["prompt_revision_id"],
                              "qa_evaluation_id": cand["qa"]["id"] if cand["qa"] else None,
                              "override_qa": not (cand["qa"] and cand["qa"]["status"] == "recommended")})
        res = cl.post(f"/api/v1/projects/{pid}/batches/{bid}:approve-candidates",
                      json={"idempotency_key": str(uuid.uuid4()), "items": approvals}).json()
        assert all(x["ok"] for x in res["results"]), res
        regen = items[4]
        cl.post(f"/api/v1/projects/{pid}/batches/{bid}:mark-regenerate", json={"mark": True, "items": [
            {"item_id": regen["id"], "expected_item_revision": regen["revision"]}]})
        d = detail(cl, pid, bid)
        build = cl.post(f"/api/v1/projects/{pid}/batches/{bid}:build-approved", json={
            "idempotency_key": str(uuid.uuid4()), "items": [{"item_id": i["id"], "approval_id": i["approval"],
                                                           "expected_item_revision": i["revision"]}
                                                          for i in d["items"] if i["legal"]["build"]]}).json()
        assert len([x for x in build["results"] if x["ok"]]) == 4, build
        old_set = regen["candidate_set"]["id"]
        d = detail(cl, pid, bid)
        target = next(i for i in d["items"] if i["id"] == regen["id"])
        cl.post(f"/api/v1/projects/{pid}/batches/{bid}:regenerate", json={"idempotency_key": str(uuid.uuid4()),
                "items": [{"item_id": target["id"], "expected_item_revision": target["revision"]}]})
        wait(cl, lambda: ops_idle(cl, pid), 1800, "build + regeneration")
        d = detail(cl, pid, bid)
        built = [i for i in d["items"] if i["build"] and i["build"]["result"] == "valid"]
        assert len(built) == 4, "strict: 4 valid builds"
        again = next(i for i in d["items"] if i["id"] == regen["id"])
        assert again["candidate_set"]["id"] != old_set and len(again["candidate_set"]["candidates"]) == 4
        assert sum(1 for i in d["items"] if not i["approval"] and not i["regen_requested"]) == 3  # 2 + regenerated
        acc = cl.post(f"/api/v1/projects/{pid}/batches/{bid}:accept-builds", json={
            "idempotency_key": str(uuid.uuid4()), "items": [{"item_id": i["id"], "build_run_id": i["current_build"],
                                                           "expected_item_revision": i["revision"]} for i in built]}).json()
        assert all(x["ok"] for x in acc["results"]), acc
        prev = cl.get(f"/api/v1/projects/{pid}/batches/{bid}/publish-preview").json()["items"]
        cl.post(f"/api/v1/projects/{pid}/batches/{bid}:publish", json={"idempotency_key": str(uuid.uuid4()), "items": [
            {"item_id": p["item_id"], "build_run_id": p["build_run_id"], "expected_item_revision": p["expected_item_revision"]}
            for p in prev]})
        wait(cl, lambda: ops_idle(cl, pid), 300, "publication")
        lib = cl.get(f"/api/v1/projects/{pid}/assets").json()
        assert lib["total"] == 4, "strict: 4 published versions"
        for a in lib["items"]:
            v = cl.get(f"/api/v1/projects/{pid}/assets/{a['asset_id']}").json()["shown_version"]
            assert v["licence"]["status"] in ("review", "cleared")  # real route: ComfyUI GPL review + Lightning review
            assert v["sources"]["candidate_id"] and v["validation"]["ok"] is True
    verify = subprocess.run(shlex.split(os.environ.get("VERIFY_CMD", "podman exec assetstudio_studio_1 assetstudio storage verify"))
                            + [pid], capture_output=True, text=True)
    assert verify.returncode == 0, verify.stdout + verify.stderr


@pytest.mark.skipif(os.environ.get("GPU_QUALITY") != "1", reason="set GPU_QUALITY=1 for the 50-step default path")
def test_quality_preset_single_item(project: str) -> None:
    pid = project
    with c() as cl:
        cfg = cl.get(f"/api/v1/projects/{pid}/config").json()["config"]
        cfg["categories"].append({"id": "concept_hq", "slug": "concept_hq", "label": "Concept HQ",
                                  "defaults": {"kind": "concept_art", "recipe_id": "concept.default"}})
        cfg["pipelines"]["concept.default"]["parameters"]["speed_preset"] = "quality"
        cl.patch(f"/api/v1/projects/{pid}/config", json={"expected_revision": cfg["revision"], "config": cfg})
        bid = cl.post(f"/api/v1/projects/{pid}/batches", json={"title": "quality", "category_id": "concept_hq",
                      "idempotency_key": str(uuid.uuid4()), "items": [{"name": "Watermill", "brief": "old watermill"}]}).json()["batch"]["id"]
        wait(cl, lambda: ops_idle(cl, pid), 600, "enhance")
        it = detail(cl, pid, bid)["items"][0]
        cl.post(f"/api/v1/projects/{pid}/batches/{bid}:confirm-and-generate", json={"idempotency_key": str(uuid.uuid4()),
                "items": [{"item_id": it["id"], "prompt_revision_id": it["current_prompt"], "expected_item_revision": it["revision"]}]})
        wait(cl, lambda: ops_idle(cl, pid), 1800, "quality generation")
        it = detail(cl, pid, bid)["items"][0]
        assert len(it["candidate_set"]["candidates"]) == 4
        assert it["candidate_set"]["generation"]["params"]["steps"] == 50
