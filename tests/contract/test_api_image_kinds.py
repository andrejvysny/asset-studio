"""Image-kind builds (sprite, icon, material) with the SIMULATED engine + fake segmentation. Not GPU evidence."""
from __future__ import annotations

import io
import json
from typing import Any

from PIL import Image

from tests.conftest import Api, new_project
from tests.contract.test_api_batches import approve, confirm_all, create, detail

KINDS = {"categories": [
    {"id": "sprites", "slug": "sprites", "label": "Sprites", "defaults": {"kind": "sprite"}},
    {"id": "icons", "slug": "icons", "label": "Icons", "defaults": {"kind": "icon"}},
    {"id": "materials", "slug": "materials", "label": "Materials", "defaults": {"kind": "material"}}]}


def _project(api: Api, pipelines: dict[str, Any] | None = None) -> str:
    pid = new_project(api)
    cfg = api.get(f"/api/v1/projects/{pid}/config")["config"]
    cfg.update(KINDS)
    if pipelines:
        cfg["pipelines"] = pipelines
    r = api.raw("PATCH", f"/api/v1/projects/{pid}/config", json={"expected_revision": 1, "config": cfg})
    assert r.status_code == 200, r.text
    return pid


def _built(api: Api, pid: str, category: str, key: str) -> tuple[str, dict]:
    bid = create(api, pid, ["Lantern"], f"batch-{key}", category=category, candidate_count=2)["batch"]["id"]
    api.wait_ops()
    confirm_all(api, pid, bid, f"confirm-{key}")
    api.wait_ops()
    item = detail(api, pid, bid)["items"][0]
    assert approve(api, pid, bid, item, 0, f"approve-{key}")["results"][0]["ok"]
    item = detail(api, pid, bid)["items"][0]
    op = api.post(f"/api/v1/projects/{pid}/batches/{bid}:build-approved", {"idempotency_key": f"build-{key}", "items": [
        {"item_id": item["id"], "approval_id": item["approval"], "expected_item_revision": item["revision"]}]})
    assert op["operation"]["kind"] == "build"
    api.wait_ops()
    return bid, detail(api, pid, bid)["items"][0]


def _content(api: Api, pid: str, art_id: str) -> bytes:
    r = api.raw("GET", f"/api/v1/projects/{pid}/artifacts/{art_id}/content")
    assert r.status_code == 200, r.text
    return r.content


def _image(api: Api, pid: str, art_id: str) -> Image.Image:
    return Image.open(io.BytesIO(_content(api, pid, art_id)))


def test_sprite_build_reuses_qa_mask_and_publishes(api: Api) -> None:
    pid = _project(api)
    bid, item = _built(api, pid, "sprites", "sprite")
    build = item["build"]
    assert build["result"] == "valid", build["validation"]
    assert build["artifacts"].keys() >= {"image", "preview", "meta"}
    im = _image(api, pid, build["artifacts"]["image"])
    assert im.mode == "RGBA" and im.size == (512, 512)
    assert im.getchannel("A").getextrema() == (0, 255)
    meta = json.loads(_content(api, pid, build["artifacts"]["meta"]))
    assert meta["mask"]["source"] == "qa_reused"  # sprite QA already segmented the approved bytes
    assert meta["pivot"]["mode"] == "bottom_center"
    assert meta["pivot"]["xy"][1] <= 512
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:accept-builds", {"idempotency_key": "accept-sprite", "items": [
        {"item_id": item["id"], "build_run_id": item["current_build"], "expected_item_revision": item["revision"]}]})
    prev = api.get(f"/api/v1/projects/{pid}/batches/{bid}/publish-preview")["items"]
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:publish", {"idempotency_key": "publish-sprite", "items": [
        {"item_id": p["item_id"], "build_run_id": p["build_run_id"], "expected_item_revision": p["expected_item_revision"]}
        for p in prev]})
    api.wait_ops()
    lib = api.get(f"/api/v1/projects/{pid}/assets")
    assert lib["total"] == 1 and lib["items"][0]["kind"] == "sprite"
    asset = api.get(f"/api/v1/projects/{pid}/assets/{lib['items'][0]['asset_id']}")
    assert {f["role"] for f in asset["files"]} == {"image", "preview", "meta"}


def test_icon_build_segments_when_qa_had_no_mask(api: Api) -> None:
    pid = _project(api, {"icon.default": {"parameters": {"sizes": [96, 48, 24]}}})
    _, item = _built(api, pid, "icons", "icon")
    build = item["build"]
    assert build["result"] == "valid", build["validation"]
    for s in (96, 48, 24):
        assert _image(api, pid, build["artifacts"][f"icon_{s}"]).size == (s, s)
    assert build["artifacts"]["image"] == build["artifacts"]["icon_96"]
    meta = json.loads(_content(api, pid, build["artifacts"]["meta"]))
    assert meta["mask"]["source"] == "computed" and meta["mask"]["simulated"] is True
    assert "cutout" in api.studio.aux.calls  # type: ignore[union-attr]


def test_material_build_keeps_original_and_checks_seams(api: Api) -> None:
    pid = _project(api)
    _, item = _built(api, pid, "materials", "material")
    build = item["build"]
    assert build["result"] == "valid", build["validation"]
    assert build["artifacts"]["base_color"] == build["inputs"]["candidate_artifact_id"]
    checks = {c["id"]: c for c in build["validation"]["checks"]}
    assert checks["seamless"]["ok"] and checks["square"]["ok"]
    meta = json.loads(_content(api, pid, build["artifacts"]["meta"]))
    assert meta["maps"] == {"base_color": "generated"}  # no invented PBR maps
    assert "not generated" in meta["derived_maps"]


def test_invalid_icon_sizes_rejected_by_config(api: Api) -> None:
    pid = new_project(api)
    cfg = api.get(f"/api/v1/projects/{pid}/config")["config"]
    cfg["pipelines"] = {"icon.default": {"parameters": {"sizes": "128,64"}}}
    r = api.raw("PATCH", f"/api/v1/projects/{pid}/config", json={"expected_revision": 1, "config": cfg})
    assert r.status_code == 422, r.text
