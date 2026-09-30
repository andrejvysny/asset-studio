"""STRICT real-stack build-profile acceptance: rock, wooden prop and tree through Qwen-Image -> TRELLIS.2 -> clean GLB
with their build profiles, then an A/B rebuild of the SAME sampled raw with default policies (no resampling).

Records per asset: processed components/triangles, alpha decision (auto: measured transparent texel fraction),
alphaMode/doubleSided, roughness before/after the clamp, previews. Evidence is CPU previews, not a game-engine render.
Artifacts: tests/gpu/artifacts/profiles/ (git-ignored).
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from assetstudio_processing.materials import read_glb

from tests.gpu.test_acceptance import c, detail, ops_idle, wait
from tests.gpu.test_model3d import FAST, _approve_best, _meta

pytestmark = pytest.mark.gpu
OUT = Path(__file__).parent / "artifacts" / "profiles"
PROFILES = {
    "stone": {"label": "Stone", "material": {"metallic": 0.0, "roughness_min": 0.7}},
    "painted": {"label": "Painted wood", "material": {"metallic": 0.0, "roughness_min": 0.75, "double_sided": False}},
    "foliage": {"label": "Foliage", "geometry": {"small_components": "preserve", "fill_holes": "disabled",
                                                 "expect_single_component": False},
                "material": {"alpha_mode": "auto", "alpha_cutoff": 0.5, "double_sided": True, "metallic": 0.0,
                             "roughness_min": 0.8}},
}
ASSETS = [
    ("rocks", "stone", "Mossy boulder", "stylized mossy boulder, chunky readable shapes, hand-painted look"),
    ("props", "painted", "Wooden barrel", "stylized wooden barrel with iron hoops, hand-painted look"),
    ("trees", "foliage", "Oak tree", "stylized small oak tree with a round leafy canopy, hand-painted look"),
]
DEFAULTS = {"small_components": "remove", "fill_holes": "upstream", "alpha_mode": "opaque", "double_sided": False}


@pytest.fixture(scope="module")
def project() -> str:
    with c() as cl:
        assert cl.get("/api/health").json()["simulated"] is False, "must not run on the simulated engine"
        w3d = next(s for s in cl.get("/api/v1/runtime").json()["services"] if s["name"] == "worker3d")
        assert w3d["ready"], f"3D worker not ready: {w3d['problems']}"
        pid = cl.post("/api/v1/projects", json={"name": f"gpu-profiles-{uuid.uuid4().hex[:6]}"}).json()["id"]
        cfg = cl.get(f"/api/v1/projects/{pid}/config").json()["config"]
        cfg["build_profiles"] = PROFILES
        cfg["categories"] = [{"id": cat, "slug": cat, "label": cat.title(), "defaults": {
            "kind": "model3d", "build_profile": prof, "budget": {"triangles": {"max": 40000}}}}
            for cat, prof, _, _ in ASSETS]
        cfg["pipelines"] = {"model3d.default": {"parameters": FAST}}
        r = cl.patch(f"/api/v1/projects/{pid}/config", json={"expected_revision": 1, "config": cfg})
        assert r.status_code == 200, r.text
        return pid


def _glb_material(cl: Any, pid: str, build: dict) -> dict:
    data = cl.get(f"/api/v1/projects/{pid}/artifacts/{build['artifacts']['model']}/content").content
    m = read_glb(data)[0]["materials"][0]
    return {"alphaMode": m.get("alphaMode", "OPAQUE"), "alphaCutoff": m.get("alphaCutoff"),
            "doubleSided": m.get("doubleSided", False), "pbr": m.get("pbrMetallicRoughness", {})}


def _summary(cl: Any, pid: str, b: dict, tag: str) -> dict:
    meta = _meta(cl, pid, b)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{tag}.png").write_bytes(cl.get(f"/api/v1/projects/{pid}/artifacts/{b['artifacts']['preview']}/content")
                                     .content)
    mat = meta.get("material") or {}
    first = (mat.get("materials") or [{}])[0]
    return {"build": b["id"], "result": b["result"], "mesh": meta.get("mesh"),
            "geometry_policy": (meta.get("export") or {}).get("geometry_policy"),
            "material_glb": _glb_material(cl, pid, b), "auto": first.get("auto"),
            "roughness_before": first.get("roughness_before"), "roughness_after": first.get("roughness_after"),
            "checks": {x["id"]: (x["ok"], x.get("detail", "")) for x in b["validation"]["checks"]}}


def _build_asset(cl: Any, pid: str, cat: str, name: str, brief: str) -> tuple[str, dict]:
    bid = cl.post(f"/api/v1/projects/{pid}/batches", json={
        "title": name, "category_id": cat, "idempotency_key": str(uuid.uuid4()),
        "items": [{"name": name, "brief": brief}]}).json()["batch"]["id"]
    wait(cl, lambda: ops_idle(cl, pid), 900, "enhancement")
    it = detail(cl, pid, bid)["items"][0]
    cl.post(f"/api/v1/projects/{pid}/batches/{bid}:confirm-and-generate", json={
        "idempotency_key": str(uuid.uuid4()), "items": [{"item_id": it["id"], "prompt_revision_id": it["current_prompt"],
                                                       "expected_item_revision": it["revision"]}]})
    wait(cl, lambda: ops_idle(cl, pid), 1800, "generation + QA")
    it = _approve_best(cl, pid, bid)
    cl.post(f"/api/v1/projects/{pid}/batches/{bid}:build-approved", json={"idempotency_key": str(uuid.uuid4()),
            "items": [{"item_id": it["id"], "approval_id": it["approval"], "expected_item_revision": it["revision"]}]})
    wait(cl, lambda: ops_idle(cl, pid), 2400, "3D build")
    return bid, detail(cl, pid, bid)["items"][0]


def _rebuild_defaults(cl: Any, pid: str, bid: str) -> dict:
    it = detail(cl, pid, bid)["items"][0]
    cl.post(f"/api/v1/projects/{pid}/batches/{bid}:build-approved", json={"idempotency_key": str(uuid.uuid4()),
            "items": [{"item_id": it["id"], "approval_id": it["approval"], "expected_item_revision": it["revision"],
                       "mode": "rebuild", "overrides": DEFAULTS}]})
    wait(cl, lambda: ops_idle(cl, pid), 1200, "A/B rebuild")
    return detail(cl, pid, bid)["items"][0]


def test_build_profiles_real_stack(project: str) -> None:
    pid, report = project, {}
    with c() as cl:
        for cat, prof, name, brief in ASSETS:
            t0 = time.monotonic()
            bid, it = _build_asset(cl, pid, cat, name, brief)
            b = it["build"]
            assert b and b["status"] == "succeeded" and b["result"] == "valid", f"strict: {it['tasks']} {b}"
            assert "material" in b["checkpoints"], "profile material stage did not run"
            prof_sum = _summary(cl, pid, b, f"{cat}-profile")
            assert prof_sum["checks"]["material_policy"][0], prof_sum["checks"]
            rmin = PROFILES[prof]["material"]["roughness_min"]
            assert prof_sum["roughness_after"]["min"] >= rmin - 1 / 255, prof_sum["roughness_after"]
            assert prof_sum["material_glb"]["pbr"].get("metallicFactor") == 0.0
            entry: dict[str, Any] = {"build_s": round(time.monotonic() - t0), "profile": prof_sum}
            if prof == "foliage":
                t1 = time.monotonic()
                it2 = _rebuild_defaults(cl, pid, bid)
                b2 = it2["build"]
                assert b2["id"] != b["id"] and b2["result"] == "valid", b2["validation"]
                assert b2["checkpoints"]["sample"]["outputs"] == b["checkpoints"]["sample"]["outputs"], "resampled"
                entry["default_rebuild"] = {"rebuild_s": round(time.monotonic() - t1),
                                            **_summary(cl, pid, b2, f"{cat}-default")}
            report[name] = entry
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
