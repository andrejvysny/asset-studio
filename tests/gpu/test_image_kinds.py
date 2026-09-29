"""STRICT real-stack Phase 5 check: sprite + icon + material batches with real generation, QA and segmentation.

Sprite and icon must build valid and publish. Material must build; its seam check is a real structural verdict, so an
invalid material is reported with its ratio (the check working), and only a valid one is published.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import uuid

import pytest

from tests.gpu.test_acceptance import c, detail, ops_idle, wait

pytestmark = pytest.mark.gpu
FAST = {"speed_preset": "lightning_8step", "width": 1024, "height": 1024, "candidate_count": 2}
CASES = [("sprites", "Brass lantern", "small brass oil lantern with a glass chimney"),
         ("icons", "Healing potion", "round glass bottle with red liquid and a cork"),
         ("materials", "Mossy cobblestone", "mossy cobblestone pavement")]


@pytest.fixture(scope="module")
def project() -> str:
    with c() as cl:
        assert cl.get("/api/health").json()["simulated"] is False, "must not run on the simulated engine"
        pid = cl.post("/api/v1/projects", json={"name": f"gpu-kinds-{uuid.uuid4().hex[:6]}"}).json()["id"]
        cfg = cl.get(f"/api/v1/projects/{pid}/config").json()["config"]
        cfg["categories"] = [{"id": k, "slug": k, "label": k.title(), "defaults": {"kind": kind}} for k, kind in
                             (("sprites", "sprite"), ("icons", "icon"), ("materials", "material"))]
        cfg["pipelines"] = {r: {"parameters": FAST} for r in ("sprite.default", "icon.default", "material.default")}
        r = cl.patch(f"/api/v1/projects/{pid}/config", json={"expected_revision": 1, "config": cfg})
        assert r.status_code == 200, r.text
        return pid


def _to_build(cl, pid: str, category: str, name: str, brief: str) -> tuple[str, dict]:
    bid = cl.post(f"/api/v1/projects/{pid}/batches", json={
        "title": f"GPU {category}", "category_id": category, "idempotency_key": str(uuid.uuid4()),
        "items": [{"name": name, "brief": brief}]}).json()["batch"]["id"]
    wait(cl, lambda: ops_idle(cl, pid), 900, f"{category} enhancement")
    it = detail(cl, pid, bid)["items"][0]
    assert it["prompt"]["origin"] == "enhanced" and not it["prompt"]["enhancer"].get("simulated")
    cl.post(f"/api/v1/projects/{pid}/batches/{bid}:confirm-and-generate", json={
        "idempotency_key": str(uuid.uuid4()), "items": [{"item_id": it["id"], "prompt_revision_id": it["current_prompt"],
                                                       "expected_item_revision": it["revision"]}]})
    wait(cl, lambda: ops_idle(cl, pid), 1800, f"{category} generation + QA")
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
    it = detail(cl, pid, bid)["items"][0]
    cl.post(f"/api/v1/projects/{pid}/batches/{bid}:build-approved", json={"idempotency_key": str(uuid.uuid4()), "items": [
        {"item_id": it["id"], "approval_id": it["approval"], "expected_item_revision": it["revision"]}]})
    wait(cl, lambda: ops_idle(cl, pid), 600, f"{category} build")
    it = detail(cl, pid, bid)["items"][0]
    assert it["build"] and it["build"]["status"] == "succeeded", f"{category} build did not run: {it['tasks']}"
    return bid, it


def _publish(cl, pid: str, bid: str, it: dict) -> None:
    cl.post(f"/api/v1/projects/{pid}/batches/{bid}:accept-builds", json={"idempotency_key": str(uuid.uuid4()), "items": [
        {"item_id": it["id"], "build_run_id": it["current_build"], "expected_item_revision": it["revision"]}]})
    prev = cl.get(f"/api/v1/projects/{pid}/batches/{bid}/publish-preview").json()["items"]
    cl.post(f"/api/v1/projects/{pid}/batches/{bid}:publish", json={"idempotency_key": str(uuid.uuid4()), "items": [
        {"item_id": p["item_id"], "build_run_id": p["build_run_id"], "expected_item_revision": p["expected_item_revision"]}
        for p in prev]})
    wait(cl, lambda: ops_idle(cl, pid), 300, "publish")
    assert detail(cl, pid, bid)["items"][0]["published"], "strict: publication missing"


def _meta(cl, pid: str, it: dict) -> dict:
    return json.loads(cl.get(f"/api/v1/projects/{pid}/artifacts/{it['build']['artifacts']['meta']}/content").content)


def test_sprite_icon_material_real_stack(project: str) -> None:
    pid = project
    with c() as cl:
        report = {}
        for category, name, brief in CASES:
            bid, it = _to_build(cl, pid, category, name, brief)
            b = it["build"]
            meta = _meta(cl, pid, it)
            report[category] = {"result": b["result"], "checks": {x["id"]: x.get("detail", x["ok"])
                                                                  for x in b["validation"]["checks"]},
                                "mask": meta.get("mask"), "seam": meta.get("seam")}
            if category == "materials":
                assert "seamless" in report[category]["checks"]
                if b["result"] != "valid":
                    continue  # the seam gate refused a non-tiling image: reported, not published
            else:
                assert b["result"] == "valid", f"{category}: {b['validation']}"
                assert meta["mask"]["simulated"] is False if meta["mask"]["source"] == "computed" else True
            _publish(cl, pid, bid, it)
        print(json.dumps(report, indent=2))
        assert report["sprites"]["mask"]["source"] == "qa_reused"  # sprite QA segmented the approved bytes
        assert report["icons"]["mask"]["source"] == "computed"  # icon QA has no mask rules: build segments on GPU1
    verify = subprocess.run(shlex.split(os.environ.get("VERIFY_CMD", "podman exec assetstudio_studio_1 assetstudio storage verify"))
                            + [pid], capture_output=True, text=True, timeout=600)
    assert verify.returncode == 0, verify.stdout + verify.stderr
