"""STRICT real-stack 3D acceptance: Qwen-Image candidates -> QA (BiRefNet + VLM) -> TRELLIS.2 + DINOv3 -> clean GLB.

Checks the GPU1 handoff (aux <-> worker3d, acknowledged unloads), stored raw intermediate, re-export without
resampling, structural validation, triangle budget, preview, publication, licence provenance and storage hashes.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
import uuid

import pytest

from tests.gpu.test_acceptance import c, detail, ops_idle, wait

pytestmark = pytest.mark.gpu
FAST = {"speed_preset": "lightning_8step", "width": 1024, "height": 1024, "candidate_count": 2}


@pytest.fixture(scope="module")
def project() -> str:
    with c() as cl:
        assert cl.get("/api/health").json()["simulated"] is False, "must not run on the simulated engine"
        rt = cl.get("/api/v1/runtime").json()
        w3d = next(s for s in rt["services"] if s["name"] == "worker3d")
        assert w3d["ready"], f"3D worker not ready: {w3d['problems']}"
        pid = cl.post("/api/v1/projects", json={"name": f"gpu-3d-{uuid.uuid4().hex[:6]}"}).json()["id"]
        cfg = cl.get(f"/api/v1/projects/{pid}/config").json()["config"]
        cfg["categories"] = [{"id": "props", "slug": "props", "label": "Props", "defaults": {
            "kind": "model3d", "budget": {"triangles": {"min": 2000, "max": 40000}}}}]
        cfg["pipelines"] = {"model3d.default": {"parameters": FAST}}
        r = cl.patch(f"/api/v1/projects/{pid}/config", json={"expected_revision": 1, "config": cfg})
        assert r.status_code == 200, r.text
        return pid


def _approve_best(cl, pid: str, bid: str) -> dict:
    it = detail(cl, pid, bid)["items"][0]
    cands = it["candidate_set"]["candidates"]
    assert len(cands) == 2 and all(x["qa"] for x in cands)
    cand = next((x for x in cands if x["qa"]["status"] == "recommended"), cands[0])
    res = cl.post(f"/api/v1/projects/{pid}/batches/{bid}:approve-candidates", json={
        "idempotency_key": str(uuid.uuid4()), "items": [{
            "item_id": it["id"], "expected_item_revision": it["revision"], "candidate_set_id": it["candidate_set"]["id"],
            "candidate_id": cand["id"], "image_sha256": cand["sha256"],
            "prompt_revision_id": it["candidate_set"]["prompt_revision_id"], "qa_evaluation_id": cand["qa"]["id"],
            "override_qa": cand["qa"]["status"] != "recommended"}]}).json()
    assert res["results"][0]["ok"], res
    return detail(cl, pid, bid)["items"][0]


def _meta(cl, pid: str, build: dict) -> dict:
    return json.loads(cl.get(f"/api/v1/projects/{pid}/artifacts/{build['artifacts']['meta']}/content").content)


def test_model3d_real_stack(project: str) -> None:
    pid = project
    with c() as cl:
        bid = cl.post(f"/api/v1/projects/{pid}/batches", json={
            "title": "GPU 3D", "category_id": "props", "idempotency_key": str(uuid.uuid4()),
            "items": [{"name": "Wooden supply crate", "brief": "small wooden supply crate with iron corner brackets"}]}
        ).json()["batch"]["id"]
        wait(cl, lambda: ops_idle(cl, pid), 900, "enhancement")
        it = detail(cl, pid, bid)["items"][0]
        cl.post(f"/api/v1/projects/{pid}/batches/{bid}:confirm-and-generate", json={
            "idempotency_key": str(uuid.uuid4()), "items": [{"item_id": it["id"], "prompt_revision_id": it["current_prompt"],
                                                           "expected_item_revision": it["revision"]}]})
        wait(cl, lambda: ops_idle(cl, pid), 1800, "generation + QA")
        it = _approve_best(cl, pid, bid)
        t0 = time.monotonic()
        cl.post(f"/api/v1/projects/{pid}/batches/{bid}:build-approved", json={"idempotency_key": str(uuid.uuid4()),
                "items": [{"item_id": it["id"], "approval_id": it["approval"], "expected_item_revision": it["revision"]}]})
        wait(cl, lambda: ops_idle(cl, pid), 2400, "3D build")
        build_s = round(time.monotonic() - t0)
        it = detail(cl, pid, bid)["items"][0]
        b = it["build"]
        assert b and b["status"] == "succeeded" and b["result"] == "valid", f"strict: {it['tasks']} {b}"
        assert b["artifacts"].keys() >= {"model", "raw", "cutout", "preview", "meta"}
        meta = _meta(cl, pid, b)
        assert meta["generation"]["engine"] == "TRELLIS.2" and meta["export"]["exporter"] == "clean"
        assert meta["budget"]["effective"] == 40000 and meta["mask"]["source"] == "qa_reused"
        assert 2000 <= meta["mesh"]["triangles"] <= 40000, meta["mesh"]
        lanes = cl.get("/api/v1/runtime").json()["lanes"]["gpu1"]
        assert lanes["state"] == "owned" and lanes["owner"] == "worker3d"

        t1 = time.monotonic()
        cl.post(f"/api/v1/projects/{pid}/batches/{bid}:reexport", json={"idempotency_key": str(uuid.uuid4()), "items": [
            {"item_id": it["id"], "build_run_id": b["id"], "expected_item_revision": it["revision"],
             "overrides": {"texture_size": 1024}}]})
        wait(cl, lambda: ops_idle(cl, pid), 900, "re-export")
        reexport_s = round(time.monotonic() - t1)
        it = detail(cl, pid, bid)["items"][0]
        b2 = it["build"]
        assert b2["id"] != b["id"] and b2["result"] == "valid" and b2["artifacts"]["raw"] == b["artifacts"]["raw"]
        meta2 = _meta(cl, pid, b2)
        assert meta2["generation"] == {"reused_raw_from": b["id"]} and meta2["export"]["texture_size"] == 1024

        cl.post(f"/api/v1/projects/{pid}/batches/{bid}:accept-builds", json={"idempotency_key": str(uuid.uuid4()),
                "items": [{"item_id": it["id"], "build_run_id": it["current_build"], "expected_item_revision": it["revision"]}]})
        prev = cl.get(f"/api/v1/projects/{pid}/batches/{bid}/publish-preview").json()["items"]
        cl.post(f"/api/v1/projects/{pid}/batches/{bid}:publish", json={"idempotency_key": str(uuid.uuid4()), "items": [
            {"item_id": p["item_id"], "build_run_id": p["build_run_id"], "expected_item_revision": p["expected_item_revision"]}
            for p in prev]})
        wait(cl, lambda: ops_idle(cl, pid), 300, "publish")
        lib = cl.get(f"/api/v1/projects/{pid}/assets").json()
        assert lib["total"] == 1, "strict: 1 published 3D asset"
        asset = cl.get(f"/api/v1/projects/{pid}/assets/{lib['items'][0]['asset_id']}").json()
        lic = {x["id"]: x["status"] for x in asset["shown_version"]["licence"]["components"]}
        assert lic.get("exporter_clean") == "cleared" and "nvdiffrast" not in lic and "dinov3_vitl16" in lic
        print(json.dumps({"build_s": build_s, "reexport_s": reexport_s, "generation": meta["generation"],
                          "export": meta["export"], "mesh": meta["mesh"], "budget": meta["budget"],
                          "checks": {x["id"]: x.get("detail", x["ok"]) for x in b["validation"]["checks"]},
                          "licence": asset["shown_version"]["licence"]["status"]}, indent=2))
    verify = subprocess.run(shlex.split(os.environ.get("VERIFY_CMD", "podman exec assetstudio_studio_1 assetstudio storage verify"))
                            + [pid], capture_output=True, text=True, timeout=900)
    assert verify.returncode == 0, verify.stdout + verify.stderr
