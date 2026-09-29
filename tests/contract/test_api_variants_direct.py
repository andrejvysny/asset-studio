"""Variants Phase B2 contracts: deterministic direct-transform builds, confirmation gate, variant publication.
CPU only: no engine, aux or 3D worker call may happen anywhere in these flows."""
from __future__ import annotations

import io
import json
from typing import Any

import pytest
from assetstudio_core.domain import AssetManifest
from assetstudio_processing.raster import to_png
from assetstudio_storage.project import manifest_key, version_key
from PIL import Image

from tests.conftest import Api, new_project, png_bytes
from tests.contract.test_api_library import _glb, _import
from tests.contract.test_api_variants import _create, _draft

P = "/api/v1/projects"
V2 = "/api/v2/projects"
HEIGHT2 = {"op": "target_height", "height_m": 2.0, "anchor": "bottom_center", "units_confirmed": True}


def _jobs(api: Api, pid: str, src: dict, method_rows: list[dict], key: str) -> dict:
    d = _draft(api, pid, src, "direct_transform", f"draft-{key}", rows=method_rows,
               requested_variants=len(method_rows))
    return _create(api, pid, d, f"jobs-{key}")


def _item(api: Api, pid: str, job_id: str) -> dict:
    return api.get(f"{V2}/{pid}/jobs/{job_id}")["items"][0]


def _run(api: Api, pid: str, job_id: str, key: str, status: int | tuple[int, ...] = 202) -> Any:
    item = _item(api, pid, job_id)
    return api.post(f"{V2}/{pid}/jobs/{job_id}:run-transform", {
        "items": [{"item_id": item["id"], "expected_item_revision": item["revision"]}], "idempotency_key": f"{key}-key"},
        status=status)


def _accept(api: Api, pid: str, job_id: str, key: str) -> None:
    item = _item(api, pid, job_id)
    api.post(f"{V2}/{pid}/jobs/{job_id}:accept-builds", {"idempotency_key": f"{key}-key", "items": [
        {"item_id": item["id"], "build_run_id": item["current_build"],
         "expected_item_revision": item["revision"]}]})


def _publish(api: Api, pid: str, job_id: str, key: str) -> dict:
    prev = api.get(f"{V2}/{pid}/jobs/{job_id}/publish-preview")["items"]
    res = api.post(f"{V2}/{pid}/jobs/{job_id}:publish", {"idempotency_key": f"{key}-key", "items": [
        {"item_id": p["item_id"], "build_run_id": p["build_run_id"], "expected_item_revision":
         p["expected_item_revision"], "expected_current_version": p["current_version_id"]} for p in prev]})
    api.wait_ops()
    return res


def _built(api: Api, pid: str, job_id: str, key: str) -> dict:
    _run(api, pid, job_id, f"run-{key}")
    api.wait_ops()
    return _item(api, pid, job_id)


def _assert_no_model_calls(api: Api) -> None:
    st = api.studio
    assert st.engine.calls == [] and st.aux.calls == []  # type: ignore[union-attr]
    assert [c for c in st.worker3d.calls if c != "unload"] == []  # type: ignore[union-attr]


def _shown(api: Api, pid: str, asset_id: str) -> dict:
    return api.get(f"{P}/{pid}/assets/{asset_id}")


def _glb_setup(api: Api, rows: list[dict] | None = None) -> tuple[str, dict, dict]:
    pid = new_project(api)
    src = _import(api, pid, "Crate", _glb(), "crate.glb")
    out = _jobs(api, pid, src, rows or [{"label": "Two meters", "glb_transform": HEIGHT2}], "glb")
    return pid, src, out


def test_vt05_vd10_vp01_glb_end_to_end(make_api) -> None:
    api = make_api()
    pid, src, out = _glb_setup(api)
    ctx = api.studio.registry.get(pid)
    before = ctx.store.get(manifest_key(src["asset_id"]), AssetManifest)[0].model_dump(mode="json")
    src_bytes = ctx.store.repo.read_object(version_key(src["asset_id"], src["version_id"])).data
    jid = out["job_ids"][0]
    job = api.get(f"{V2}/{pid}/jobs/{jid}")
    assert job["next_action"] == "run transform" and job["items"][0]["stage"]["state"] == "ready to transform"
    assert job["items"][0]["legal"]["run_transform"] and not job["items"][0]["legal"]["build"]
    item = _built(api, pid, jid, "a1")
    build = item["build"]
    assert build["result"] == "valid", build["validation"]
    assert build["build"] == "direct_glb" and set(build["artifacts"]) >= {"model", "preview", "meta"}
    checks = {c["id"]: c for c in build["validation"]["checks"]}
    assert checks["height_target"]["ok"] and checks["not_duplicate"]["ok"] and checks["bin_identical"]["ok"]
    dec = item["approval_detail"]
    assert dec["gate"] == "transform_confirmation" and dec["bound"]["direct"] is True
    assert dec["bound"]["source"] == {"asset_id": src["asset_id"], "version_id": src["version_id"]}
    assert api.get(f"{V2}/{pid}/jobs/{jid}")["next_action"] != "run transform"
    _accept(api, pid, jid, "acc-a1")
    row = api.get(f"{V2}/{pid}/jobs/{jid}/publish-preview")["items"][0]
    assert row["family"]["id"] == out["family_id"] and row["derived_from"]["asset_id"] == src["asset_id"]
    assert row["derived_from"]["display_version"] == 1 and row["new_asset"]
    _publish(api, pid, jid, "pub-a1")
    published = _item(api, pid, jid)["published"]
    assert published["asset_id"] != src["asset_id"] and published["build_run_id"] == build["id"]
    asset = _shown(api, pid, published["asset_id"])
    m, v = asset["manifest"], asset["shown_version"]
    assert m["origin"] == "derived" and m["family_id"] == out["family_id"] and len(m["versions"]) == 1
    assert m["current_version_id"] == published["version_id"] and v["display_version"] == 1
    der = v["derivation"]
    assert der["source"]["asset_id"] == src["asset_id"] and der["source"]["version_id"] == src["version_id"]
    assert der["method"] == "direct_transform" and der["transform"]["height_ok"] is True
    assert der["plan_id"] == out["plan_id"] and der["family_anchor"]["anchor_asset_id"] == src["asset_id"]
    assert v["models"] == [] and v["engine"] == {} and v["sources"]["approval_id"] == dec["id"]
    assert v["parameters"]["transform"]["height_m"] == 2.0
    assert {f["role"] for f in asset["files"]} == {"model", "preview", "meta"}
    # the source is untouched (VD10/VP01)
    after = ctx.store.get(manifest_key(src["asset_id"]), AssetManifest)[0].model_dump(mode="json")
    assert after["versions"] == before["versions"] and after["current_version_id"] == before["current_version_id"]
    assert after["family_id"] == out["family_id"]
    assert ctx.store.repo.read_object(version_key(src["asset_id"], src["version_id"])).data == src_bytes
    _assert_no_model_calls(api)


def test_vp02_two_rows_two_new_assets_one_family(make_api) -> None:
    api = make_api()
    rows = [{"label": "Two", "glb_transform": HEIGHT2},
            {"label": "Big", "glb_transform": {"op": "uniform_scale", "factor": 3.0}}]
    pid, src, out = _glb_setup(api, rows)
    assert len(out["job_ids"]) == 2
    published = []
    for n, jid in enumerate(out["job_ids"]):
        assert _built(api, pid, jid, f"b{n}")["build"]["result"] == "valid"
        _accept(api, pid, jid, f"acc-b{n}")
        _publish(api, pid, jid, f"pub-b{n}")
        published.append(_item(api, pid, jid)["published"])
    assert published[0]["asset_id"] != published[1]["asset_id"]
    assert all(p["display_version"] == 1 for p in published)
    for p in published:
        assert _shown(api, pid, p["asset_id"])["manifest"]["family_id"] == out["family_id"]
    groups = api.get(f"{P}/{pid}/assets?group_by=family")["groups"]
    fam = [g for g in groups if g["type"] == "family"]
    assert len(fam) == 1 and fam[0]["total_member_count"] == 3
    _assert_no_model_calls(api)


def test_vp03_publish_replay_and_no_second_publication(make_api) -> None:
    api = make_api()
    pid, src, out = _glb_setup(api)
    jid = out["job_ids"][0]
    _built(api, pid, jid, "c1")
    _accept(api, pid, jid, "acc-c1")
    item = _item(api, pid, jid)
    body = {"idempotency_key": "pub-c1-key", "items": [
        {"item_id": item["id"], "build_run_id": item["accepted_build"], "expected_item_revision": item["revision"]}]}
    first = api.post(f"{V2}/{pid}/jobs/{jid}:publish", body)
    api.wait_ops()
    assert api.post(f"{V2}/{pid}/jobs/{jid}:publish", body) == first  # same key: recorded response
    item = _item(api, pid, jid)
    r = api.post(f"{V2}/{pid}/jobs/{jid}:publish", {"idempotency_key": "pub-c1-new", "items": [
        {"item_id": item["id"], "build_run_id": item["accepted_build"], "expected_item_revision": item["revision"]}]})
    res = r["results"][0]
    assert not res["ok"] and res["code"] == "already_published" and res["message"].startswith("this accepted")
    api.wait_ops()
    lib = api.get(f"{P}/{pid}/assets")
    assert lib["total"] == 2  # source + one variant
    asset = _shown(api, pid, item["published"]["asset_id"])
    assert len(asset["manifest"]["versions"]) == 1


def test_vt08_raster_alpha_preserved(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    buf = io.BytesIO()
    im = Image.new("RGBA", (64, 64), (200, 40, 40, 255))
    im.paste((0, 0, 0, 0), (0, 0, 32, 64))
    im.save(buf, "PNG")
    src = _import(api, pid, "Tile", buf.getvalue(), "tile.png")
    out = _jobs(api, pid, src, [{"label": "Small", "raster_transform": {
        "op": "resize_keep_aspect", "max_width": 32, "max_height": 32}}], "png")
    jid = out["job_ids"][0]
    item = _built(api, pid, jid, "d1")
    build = item["build"]
    assert build["result"] == "valid" and build["build"] == "direct_raster", build["validation"]
    checks = {c["id"]: c for c in build["validation"]["checks"]}
    assert checks["alpha_preserved"]["ok"] and checks["output_size"]["ok"]
    _accept(api, pid, jid, "acc-d1")
    _publish(api, pid, jid, "pub-d1")
    asset = _shown(api, pid, _item(api, pid, jid)["published"]["asset_id"])
    assert {f["role"] for f in asset["files"]} == {"image", "preview", "meta"}
    assert asset["manifest"]["kind"] == _shown(api, pid, src["asset_id"])["manifest"]["kind"] and asset["manifest"]["origin"] == "derived"
    art = next(f for f in asset["files"] if f["role"] == "image")
    data = api.raw("GET", f"{P}/{pid}/artifacts/{art['artifact_id']}/content").content
    res = Image.open(io.BytesIO(data))
    assert res.size == (32, 32) and res.mode == "RGBA" and res.getchannel("A").getextrema() == (0, 255)
    _assert_no_model_calls(api)


def test_noop_transform_is_invalid_unless_confirmed(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    src = _import(api, pid, "Tile", to_png_array(), "tile.png")
    same = {"op": "resize_keep_aspect", "max_width": 64, "max_height": 64}
    out = _jobs(api, pid, src, [{"label": "Same", "raster_transform": same},
                                {"label": "Same but new", "raster_transform": same, "confirm_duplicate": True}], "dup")
    bad = _built(api, pid, out["job_ids"][0], "e1")["build"]
    assert bad["result"] == "invalid"
    dup = next(c for c in bad["validation"]["checks"] if c["id"] == "not_duplicate")
    assert not dup["ok"] and "identical to the source" in dup["detail"]
    ok = _built(api, pid, out["job_ids"][1], "e2")["build"]
    assert ok["result"] == "valid"
    # unit level: an identity GLB scale is a duplicate as well
    glb_out = _jobs(api, pid, _import(api, pid, "Crate", _glb(), "crate.glb"),
                    [{"label": "Identity", "glb_transform": {"op": "uniform_scale", "factor": 1.0}}], "dup2")
    inv = _built(api, pid, glb_out["job_ids"][0], "e3")["build"]
    assert inv["result"] == "invalid"
    assert not next(c for c in inv["validation"]["checks"] if c["id"] == "not_duplicate")["ok"]


def to_png_array() -> bytes:
    import numpy as np

    return to_png(np.full((64, 64, 4), (10, 200, 30, 255), dtype=np.uint8))


def test_vt13_preview_failure_is_isolated_and_retryable(make_api, monkeypatch: pytest.MonkeyPatch) -> None:
    from assetstudio_server.coordinator.builds import direct

    def boom(data: bytes, size: int = 384) -> bytes:
        raise RuntimeError("renderer exploded")
    monkeypatch.setattr(direct, "preview_png", boom)
    api = make_api()
    pid, src, out = _glb_setup(api)
    jid = out["job_ids"][0]
    item = _built(api, pid, jid, "f1")
    assert item["build"]["result"] == "valid" and item["build"]["preview"] == "failed"
    assert item["legal"]["retry_preview"]
    api.post(f"{V2}/{pid}/jobs/{jid}:retry-preview", {"idempotency_key": "retry-f1-key", "items": [
        {"item_id": item["id"], "expected_item_revision": item["revision"]}]})
    api.wait_ops()
    build = _item(api, pid, jid)["build"]
    assert build["preview"] == "available" and "preview" in build["artifacts"]


def test_vp04_source_licence_carried_verbatim(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    src = _import(api, pid, "Crate", _glb(), "crate.glb")
    ctx = api.studio.registry.get(pid)
    src_ver = ctx.store.repo.read_object(version_key(src["asset_id"], src["version_id"])).data
    source_licence = json.loads(src_ver)["licence"]
    out = _jobs(api, pid, src, [{"label": "Two", "glb_transform": HEIGHT2}], "lic")
    jid = out["job_ids"][0]
    _built(api, pid, jid, "g1")
    _accept(api, pid, jid, "acc-g1")
    _publish(api, pid, jid, "pub-g1")
    v = _shown(api, pid, _item(api, pid, jid)["published"]["asset_id"])["shown_version"]
    assert v["licence"]["source_licence"] == source_licence
    assert v["licence"]["derivation"] == "deterministic CPU transform" and v["licence"]["components"] == []
    assert v["licence"].get("status", "unknown") == source_licence.get("status", "unknown")


def test_direct_and_generative_guards(make_api) -> None:
    api = make_api()
    pid, src, out = _glb_setup(api)
    jid = out["job_ids"][0]
    item = _item(api, pid, jid)
    body = {"items": [{"item_id": item["id"], "expected_item_revision": item["revision"]}],
            "idempotency_key": "guard-key-1"}
    for path, payload in (("approve-candidates", None), ("build-approved", None)):
        payload = {"idempotency_key": f"guard-{path}", "items": [
            {**body["items"][0], "approval_id": "dec_x", "candidate_set_id": "x", "candidate_id": "x",
             "image_sha256": "0" * 64, "prompt_revision_id": "x", "qa_evaluation_id": None}]}
        r = api.raw("POST", f"{V2}/{pid}/jobs/{jid}:{path}", json=payload)
        assert r.status_code == 422 and r.json()["error"]["code"] == "not_applicable", r.text
    png = _import(api, pid, "Tile", png_bytes(), "tile.png")
    gen = _draft(api, pid, png, "image_edit", "draft-gen", rows=[{"label": "V", "change_request": "recolor"}])
    api.post(f"{P}/{pid}/variant-drafts/{gen['id']}:prepare-references")
    gen = api.get(f"{P}/{pid}/variant-drafts/{gen['id']}")
    gjid = _create(api, pid, gen, "jobs-gen")["job_ids"][0]
    gitem = _item(api, pid, gjid)
    assert not gitem["legal"]["run_transform"]
    r = api.raw("POST", f"{V2}/{pid}/jobs/{gjid}:run-transform", json={
        "items": [{"item_id": gitem["id"], "expected_item_revision": gitem["revision"]}],
        "idempotency_key": "guard-key-2"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_direct"
    r = api.raw("POST", f"{V2}/{pid}/jobs/{jid}:run-transform", json={**body, "items": [
        {"item_id": item["id"], "expected_item_revision": item["revision"] + 5}]})
    assert r.status_code == 202 and r.json()["results"][0]["code"] == "stale_item"
    assert api.studio.journal.tasks.list(project_id=pid) == []


def test_run_transform_is_idempotent_and_single(make_api) -> None:
    api = make_api()
    pid, src, out = _glb_setup(api)
    jid = out["job_ids"][0]
    item = _item(api, pid, jid)
    body = {"items": [{"item_id": item["id"], "expected_item_revision": item["revision"]}],
            "idempotency_key": "run-once-key"}
    first = api.post(f"{V2}/{pid}/jobs/{jid}:run-transform", body)
    assert api.post(f"{V2}/{pid}/jobs/{jid}:run-transform", body) == first
    api.wait_ops()
    item = _item(api, pid, jid)
    assert len(item["build_history"]) == 1 and len(item["decisions"]) == 1
    again = api.post(f"{V2}/{pid}/jobs/{jid}:run-transform", {
        "items": [{"item_id": item["id"], "expected_item_revision": item["revision"]}],
        "idempotency_key": "run-twice-key"})
    assert again["results"][0]["code"] == "already_built"


def test_source_integrity_failure_is_reported_per_item(make_api) -> None:
    api = make_api()
    pid, src, out = _glb_setup(api)
    ctx = api.studio.registry.get(pid)
    jid = out["job_ids"][0]
    art = ctx.store.artifact(next(f["artifact_id"] for f in _shown(api, pid, src["asset_id"])["files"]
                                  if f["role"] == "model"))
    path = ctx.store.repo._blob_path(art.sha256)  # type: ignore[attr-defined]
    data = bytearray(path.read_bytes())
    data[-1] ^= 0xFF
    path.chmod(0o644)
    path.write_bytes(bytes(data))
    r = _run(api, pid, jid, "integrity")
    assert r["results"][0]["ok"] is False and r["results"][0]["code"] == "source_integrity_failed"
    assert api.studio.journal.tasks.list(project_id=pid) == [] and _item(api, pid, jid)["build_history"] == []


def test_batch_of_direct_jobs_queues_no_enhance(make_api) -> None:
    api = make_api()
    rows = [{"label": "Two", "glb_transform": HEIGHT2},
            {"label": "Big", "glb_transform": {"op": "uniform_scale", "factor": 3.0}}]
    pid, src, out = _glb_setup(api, rows)
    plan = api.post(f"{V2}/{pid}/batches/{out['batch_id']}:plan", {})
    entries = [e for j in plan["jobs"] for e in j["items"]]
    assert len(entries) == 2 and all(e["action"] == "at_gate" for e in entries)
    assert all("no model stages" in e["reason"] for e in entries) and plan["counts"]["enhance"] == 0
    started = api.post(f"{V2}/{pid}/batches/{out['batch_id']}:start", {
        "plan_id": plan["plan_id"], "plan_sha256": plan["plan_sha256"], "idempotency_key": "start-direct-1"})
    assert started["tasks"] == []
    api.wait_ops()
    assert api.studio.journal.tasks.list(project_id=pid) == []
    _assert_no_model_calls(api)
