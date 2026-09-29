"""3D lifecycle with the SIMULATED engine + fake worker3d (M01-M07 contracts). Not GPU or reconstruction evidence."""
from __future__ import annotations

import json

from assetstudio_server.adapters.fake import FakeAux, FakeEngine
from assetstudio_server.studio import build_studio

from tests.conftest import Api, make_settings
from tests.contract.test_api_batches import approve, confirm_all, create, detail, setup_project


def _budget_project(api: Api) -> str:
    pid = setup_project(api)
    cfg = api.get(f"/api/v1/projects/{pid}/config")["config"]
    cfg["categories"].append({"id": "crates", "parent_id": "props", "slug": "crates", "label": "Crates",
                              "defaults": {"budget": {"triangles": {"min": 100, "max": 5000}}}})
    r = api.raw("PATCH", f"/api/v1/projects/{pid}/config", json={"expected_revision": 2, "config": cfg})
    assert r.status_code == 200, r.text
    return pid


def _built(api: Api, pid: str, category: str, key: str) -> tuple[str, dict]:
    bid = create(api, pid, ["Crate"], f"b3d-{key}", category=category, candidate_count=2)["batch"]["id"]
    api.wait_ops()
    confirm_all(api, pid, bid, f"c3d-{key}")
    api.wait_ops()
    it = detail(api, pid, bid)["items"][0]
    assert approve(api, pid, bid, it, 0, f"a3d-{key}")["results"][0]["ok"]
    it = detail(api, pid, bid)["items"][0]
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:build-approved", {"idempotency_key": f"bb3d-{key}", "items": [
        {"item_id": it["id"], "approval_id": it["approval"], "expected_item_revision": it["revision"]}]})
    api.wait_ops()
    return bid, detail(api, pid, bid)["items"][0]


def _meta(api: Api, pid: str, build: dict) -> dict:
    return json.loads(api.raw("GET", f"/api/v1/projects/{pid}/artifacts/{build['artifacts']['meta']}/content").content)


def test_model3d_build_stores_raw_validates_and_publishes(api: Api) -> None:
    pid = _budget_project(api)
    bid, it = _built(api, pid, "crates", "single-1")
    b = it["build"]
    assert b["result"] == "valid", b["validation"]
    assert b["artifacts"].keys() >= {"model", "raw", "cutout", "preview", "meta"}
    checks = {c["id"]: c for c in b["validation"]["checks"]}
    assert checks["material_texture_present"]["ok"] and checks["uvs_present"]["ok"]
    assert checks["triangle_budget"]["ok"] and checks["triangle_budget"]["advisory"]
    meta = _meta(api, pid, b)
    assert meta["budget"]["requested"] == 5000 and meta["budget"]["source"] == "category budget"
    assert meta["budget"]["effective"] == 5000 and meta["mask"]["source"] == "qa_reused"
    assert b["inputs"]["components"][-1] == "exporter_clean"
    assert [c for c in api.studio.worker3d.calls if c != "unload"] == ["generate", "export:clean"]  # type: ignore[union-attr]
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:accept-builds", {"idempotency_key": "acc3d-single", "items": [
        {"item_id": it["id"], "build_run_id": it["current_build"], "expected_item_revision": it["revision"]}]})
    prev = api.get(f"/api/v1/projects/{pid}/batches/{bid}/publish-preview")["items"]
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:publish", {"idempotency_key": "pub3d-single", "items": [
        {"item_id": p["item_id"], "build_run_id": p["build_run_id"], "expected_item_revision": p["expected_item_revision"]}
        for p in prev]})
    api.wait_ops()
    lib = api.get(f"/api/v1/projects/{pid}/assets")
    asset = api.get(f"/api/v1/projects/{pid}/assets/{lib['items'][0]['asset_id']}")
    roles = {f["role"] for f in asset["files"]}
    assert roles >= {"model", "preview", "meta"} and not roles & {"raw", "cutout"}  # intermediates stay on the run
    assert asset["shown_version"]["sources"]["intermediates"]["raw"] == b["artifacts"]["raw"]
    lic = {c["id"] for c in asset["shown_version"]["licence"]["components"]}
    assert {"dinov3_vitl16", "trellis2", "exporter_clean"} <= lic and "nvdiffrast" not in lic


def test_reexport_reuses_raw_without_resampling(api: Api) -> None:
    pid = _budget_project(api)
    bid, it = _built(api, pid, "crates", "reexport-1")
    first = it["build"]
    r = api.raw("POST", f"/api/v1/projects/{pid}/batches/{bid}:reexport", json={"idempotency_key": "re3d-bad-1",
                "items": [{"item_id": it["id"], "build_run_id": first["id"], "expected_item_revision": it["revision"],
                           "overrides": {"seed": 3}}]})
    assert r.status_code == 422
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:reexport", {"idempotency_key": "re3d-ok-1", "items": [
        {"item_id": it["id"], "build_run_id": first["id"], "expected_item_revision": it["revision"],
         "overrides": {"texture_size": 1024, "remesh": True}}]})
    api.wait_ops()
    it2 = detail(api, pid, bid)["items"][0]
    second = it2["build"]
    assert second["id"] != first["id"] and second["result"] == "valid"
    assert second["inputs"]["reexport_of"] == first["id"]
    assert second["artifacts"]["raw"] == first["artifacts"]["raw"]  # same stored intermediate
    assert api.studio.worker3d.calls.count("generate") == 1  # type: ignore[union-attr]
    assert _meta(api, pid, second)["generation"] == {"reused_raw_from": first["id"]}


def test_model3d_build_blocked_without_worker(make_api, tmp_path) -> None:
    api = make_api(studio=build_studio(make_settings(tmp_path / "nw"), engine=FakeEngine(), aux=FakeAux()))
    pid = setup_project(api)
    bid = create(api, pid, ["Crate"], "b3d-noworker", category="props")["batch"]["id"]
    d = detail(api, pid, bid)
    assert d["recipe"]["build_available"] is False and "3D worker" in d["recipe"]["build_blocked_reason"]
    r = api.raw("POST", f"/api/v1/projects/{pid}/batches/{bid}:build-approved", json={
        "idempotency_key": "b3d-noworker-1", "items": [{"item_id": d["items"][0]["id"], "approval_id": "dec_x",
                                                  "expected_item_revision": 1}]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "build_unavailable"
