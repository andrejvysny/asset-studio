"""Variants Phase B1 contracts (VD01-VD11): capabilities, drafts, references, plan -> one-item Jobs.
SIMULATED engines only; nothing is ever queued or generated here."""
from __future__ import annotations

import io
import json
from typing import Any

from assetstudio_core.domain import AssetManifest, JobItem
from assetstudio_core.ids import derived_id
from assetstudio_core.kinds import Kind, Origin
from assetstudio_core.seeds import derive_seed
from assetstudio_server.services import commands
from assetstudio_server.services.records import item_key
from assetstudio_storage.families import attach_asset, get_family, list_families
from assetstudio_storage.project import manifest_key
from assetstudio_storage.publication import NewAsset, PublishRequest, publish
from PIL import Image

from tests.conftest import Api, new_project, png_bytes
from tests.contract.test_api_library import _glb, _import
from tests.unit.test_transforms import build_glb

P = "/api/v1/projects"
V2 = "/api/v2/projects"
RESIZE = {"op": "resize_keep_aspect", "max_width": 32, "max_height": 32}


def _draft(api: Api, pid: str, src: dict, method: str, key: str, status: int | tuple[int, ...] = 201, **kw: Any) -> Any:
    return api.post(f"{P}/{pid}/variant-drafts", {"asset_id": src["asset_id"], "version_id": src["version_id"],
                                                  "method": method, "idempotency_key": key, **kw}, status=status)


def _create(api: Api, pid: str, draft: dict, key: str, status: int | tuple[int, ...] = 201) -> Any:
    return api.post(f"{P}/{pid}/variant-drafts/{draft['id']}:create-jobs",
                    {"expected_revision": draft["revision"], "idempotency_key": key}, status=status)


def _get(api: Api, pid: str, draft_id: str) -> dict:
    return api.get(f"{P}/{pid}/variant-drafts/{draft_id}")


def _prepare(api: Api, pid: str, draft: dict) -> dict:
    api.post(f"{P}/{pid}/variant-drafts/{draft['id']}:prepare-references")
    return _get(api, pid, draft["id"])


def _rows(n: int, **extra: Any) -> list[dict]:
    return [{"label": f"V{i}", "change_request": f"make variant {i}", **extra} for i in range(n)]


def _png_src(api: Api, pid: str, name: str = "Tile", color: tuple[int, int, int] = (200, 80, 40)) -> dict:
    return _import(api, pid, name, png_bytes(color=color), f"{name}.png")


def _no_work(api: Api, pid: str) -> None:
    assert api.studio.journal.tasks.list(project_id=pid) == []


def test_vd01_vd02_single_variant_of_imported_glb(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    src = _import(api, pid, "Crate", _glb(), "crate.glb")
    caps = api.get(f"{P}/{pid}/assets/{src['asset_id']}/versions/{src['version_id']}/variant-capabilities")
    by = {m["method"]: m for m in caps["methods"]}
    assert caps["source"]["origin"] == "imported" and caps["family"] is None  # VD02: no generation Job behind it
    assert by["direct_transform"]["available"] and not by["direct_transform"]["warnings"]
    assert by["image_edit_reconstruct"]["available"] and "experimental" in by["image_edit_reconstruct"]["warnings"][0]
    assert not by["image_edit"]["available"] and by["image_edit"]["reason"] == "unsupported_configuration"
    d = _draft(api, pid, src, "image_edit_reconstruct", "draft-key-1", change_request="rust it")
    assert len(d["rows"]) == 1 and d["rows"][0]["label"] == "Variant" and d["family"]["new_name"] == "Crate"
    r = api.raw("POST", f"{P}/{pid}/variant-drafts/{d['id']}:create-jobs",
                json={"expected_revision": d["revision"], "idempotency_key": "jobs-key-0"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "references_missing"
    d = _prepare(api, pid, d)
    out = _create(api, pid, d, "jobs-key-1")
    assert out["batch_id"] is None and len(out["job_ids"]) == 1
    assert out["summary"] == {"rows": 1, "image_edits": 4, "builds": 1, "transforms": 0}
    job = api.get(f"{V2}/{pid}/jobs/{out['job_ids'][0]}")
    assert job["source"] == "variant" and job["direct"] is False and len(job["items"]) == 1
    assert job["variant"]["source_version_id"] == src["version_id"] and job["variant"]["family_id"] == out["family_id"]
    assert job["family"]["name"] == "Crate" and job["batch"] is None and job["rounds"] == 0
    assert len(api.get(f"{V2}/{pid}/jobs")["jobs"]) == 1 and api.get(f"{V2}/{pid}/batches")["batches"] == []
    store = api.studio.registry.get(pid).store
    snap = store.read_snapshot(store.get(item_key(job["id"], job["items"][0]["id"]), JobItem)[0].snapshot_sha)
    assert snap["variant"]["plan_id"] == out["plan_id"] and snap["variant"]["generation"] == "comfyui.qwen_edit_2511"
    _no_work(api, pid)


def test_vd03_six_rows_four_candidates_family_and_batch(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    src = _png_src(api, pid)
    d = _draft(api, pid, src, "image_edit", "draft-key-3", rows=_rows(6), requested_variants=6, family_name="Tiles")
    assert len(d["rows"]) == 6 and all(r["candidate_count"] == 4 for r in d["rows"])
    d = _prepare(api, pid, d)
    assert d["work"]["image_edits"] == 24
    out = _create(api, pid, d, "jobs-key-3")
    assert out["summary"]["image_edits"] == 24 and len(out["job_ids"]) == 6 and out["batch_id"]
    batch = api.get(f"{V2}/{pid}/batches/{out['batch_id']}")
    assert batch["job_ids"] == out["job_ids"] and batch["title"].startswith("Variants")
    ctx = api.studio.registry.get(pid)
    assert ctx.store.get(manifest_key(src["asset_id"]), AssetManifest)[0].family_id == out["family_id"]
    fam = get_family(ctx.store, out["family_id"])
    assert (fam.anchor_asset_id, fam.anchor_version_id, fam.name) == (src["asset_id"], src["version_id"], "Tiles")
    jobs = api.get(f"{V2}/{pid}/jobs")["jobs"]
    assert {j["batch"]["id"] for j in jobs} == {out["batch_id"]} and len(jobs) == 6
    assert api.get(f"{P}/{pid}/assets/{src['asset_id']}")["family_id"] == out["family_id"]
    assert _get(api, pid, d["id"])["materialized"]["job_ids"] == out["job_ids"]
    _no_work(api, pid)


def test_vd04_direct_needs_no_models_engine_or_aux(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    assert api.studio.aux is None and api.studio.engine is None
    pid = new_project(api)
    glb = _import(api, pid, "Crate", _glb(), "crate.glb")
    caps = api.get(f"{P}/{pid}/assets/{glb['asset_id']}/versions/{glb['version_id']}/variant-capabilities")
    by = {m["method"]: m for m in caps["methods"]}
    assert by["direct_transform"]["available"] and by["image_edit_reconstruct"]["reason"] == "missing_models"
    rows = [{"label": "Double", "glb_transform": {"op": "uniform_scale", "factor": 2.0}},
            {"label": "Half", "glb_transform": {"op": "uniform_scale", "factor": 0.5}}]
    d = _draft(api, pid, glb, "direct_transform", "draft-key-4", rows=rows, requested_variants=2)
    assert d["intent"] is None
    out = _create(api, pid, d, "jobs-key-4")
    assert out["summary"] == {"rows": 2, "image_edits": 0, "builds": 2, "transforms": 2}
    job = api.get(f"{V2}/{pid}/jobs/{out['job_ids'][0]}")
    assert job["direct"] is True and job["variant"]["method"] == "direct_transform"
    png = _png_src(api, pid)
    d2 = _draft(api, pid, png, "direct_transform", "draft-key-4b", rows=[{"label": "Small", "raster_transform": RESIZE}])
    assert len(_create(api, pid, d2, "jobs-key-4b")["job_ids"]) == 1
    _no_work(api, pid)


def test_vd05_variant_of_a_family_member_reuses_family(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    a = _png_src(api, pid, "A")
    d = _draft(api, pid, a, "direct_transform", "draft-key-5", rows=[{"label": "S", "raster_transform": RESIZE}])
    fid = _create(api, pid, d, "jobs-key-5")["family_id"]
    ctx = api.studio.registry.get(pid)
    b = _png_src(api, pid, "B", (1, 2, 3))  # stands in for a published variant (publication is Phase B2)
    ctx.index.upsert(attach_asset(ctx.store, asset_id=b["asset_id"], family_id=fid, expected_manifest_revision=None),
                     "A")
    d2 = _draft(api, pid, b, "direct_transform", "draft-key-5b", rows=[{"label": "T", "raster_transform": RESIZE}])
    assert d2["family"] == {"family_id": fid, "new_name": None}
    out = _create(api, pid, d2, "jobs-key-5b")
    plan = json.loads(ctx.store.repo.read_object(f"variant-plans/{out['plan_id']}.json").data)
    assert plan["source"]["asset_id"] == b["asset_id"] and plan["family"]["family_id"] == fid
    assert out["family_id"] == fid and len(list_families(ctx.store)) == 1


def test_vd06_new_source_version_does_not_move_bindings(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    src = _png_src(api, pid)
    d = _draft(api, pid, src, "direct_transform", "draft-key-6", rows=[{"label": "S", "raster_transform": RESIZE}])
    out = _create(api, pid, d, "jobs-key-6")
    prev = api.post(f"{P}/{pid}/imports:preview", files={"file": ("n.png", png_bytes(color=(9, 9, 9)))})
    v2 = api.post(f"{P}/{pid}/imports:commit", {
        "import_id": prev["import_id"], "name": "Tile", "kind": "concept_art", "target_asset_id": src["asset_id"],
        "expected_current_version": src["version_id"], "idempotency_key": "import-v2-key"})
    assert v2["version_id"] != src["version_id"]
    job = api.get(f"{V2}/{pid}/jobs/{out['job_ids'][0]}")
    assert job["variant"]["source_version_id"] == src["version_id"] and job["variant"]["source_display_version"] == 1
    ctx = api.studio.registry.get(pid)
    plan = json.loads(ctx.store.repo.read_object(f"variant-plans/{out['plan_id']}.json").data)
    assert plan["source"]["version_id"] == src["version_id"] and plan["sha256"] == job["variant"]["plan_sha256"]
    # a new draft against the old version is still legal (it is a published version) and binds that exact version
    assert _draft(api, pid, src, "direct_transform", "draft-key-6b")["source"]["version_id"] == src["version_id"]


def test_vd07_row_ids_stable_and_seeds_derive_from_them(make_api) -> None:
    api = make_api(coordinator=False)
    pid = new_project(api)
    src = _png_src(api, pid)
    rows = [{"label": "A", "raster_transform": RESIZE}, {"label": "B", "raster_transform": RESIZE}]
    d = _draft(api, pid, src, "direct_transform", "draft-key-7", rows=rows, requested_variants=2)
    ids = [r["id"] for r in d["rows"]]
    a, b = d["rows"]
    patched = api.raw("PATCH", f"{P}/{pid}/variant-drafts/{d['id']}", json={
        "expected_revision": d["revision"],
        "rows": [b, {"label": "C", "raster_transform": RESIZE}, a]}).json()
    assert [r["id"] for r in patched["rows"]][0::2] == [ids[1], ids[0]] and patched["rows"][1]["id"] not in ids
    stale = api.raw("PATCH", f"{P}/{pid}/variant-drafts/{d['id']}", json={
        "expected_revision": d["revision"], "request": "x"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_variant_plan"
    out = _create(api, pid, patched, "jobs-key-7")
    ctx = api.studio.registry.get(pid)
    cid = commands.command_id(ctx, "variant_create_jobs", "jobs-key-7")
    row_ids = [r["id"] for r in patched["rows"]]
    assert out["job_ids"] == [derived_id("job", cid, r) for r in row_ids]
    plan = json.loads(ctx.store.repo.read_object(f"variant-plans/{out['plan_id']}.json").data)
    for jid, rid in zip(out["job_ids"], row_ids, strict=True):
        assert api.get(f"{V2}/{pid}/jobs/{jid}")["seed_family"] == derive_seed(plan["planner"]["seed_family"], rid)
    after = api.raw("PATCH", f"{P}/{pid}/variant-drafts/{d['id']}", json={
        "expected_revision": _get(api, pid, d["id"])["revision"], "request": "late"})
    assert after.status_code == 409 and after.json()["error"]["code"] == "draft_materialized"


def _skinned_glb() -> bytes:
    return build_glb(mutate=lambda doc: doc.update(skins=[{"joints": [0]}]))


def test_vd08_precise_reasons_for_unsupported_sources(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    png = _png_src(api, pid)
    r = api.raw("POST", f"{P}/{pid}/variant-drafts", json={
        "asset_id": png["asset_id"], "version_id": png["version_id"], "method": "image_edit_reconstruct",
        "idempotency_key": "draft-key-8"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "unsupported_configuration"
    skinned = _import(api, pid, "Rigged", _skinned_glb(), "rigged.glb")
    caps = api.get(f"{P}/{pid}/assets/{skinned['asset_id']}/versions/{skinned['version_id']}/variant-capabilities")
    direct = next(m for m in caps["methods"] if m["method"] == "direct_transform")
    assert not direct["available"] and direct["reason"] == "unsupported_source_features" and "skin" in direct["message"]
    d = _draft(api, pid, skinned, "direct_transform", "draft-key-8b",
               rows=[{"label": "Big", "glb_transform": {"op": "uniform_scale", "factor": 2.0}}])
    r = api.raw("POST", f"{P}/{pid}/variant-drafts/{d['id']}:create-jobs",
                json={"expected_revision": d["revision"], "idempotency_key": "jobs-key-8"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "unsupported_source_features"
    bad = _draft(api, pid, png, "direct_transform", "draft-key-8c", rows=[{"label": "NoOp"}])
    r = api.raw("POST", f"{P}/{pid}/variant-drafts/{bad['id']}:create-jobs",
                json={"expected_revision": bad["revision"], "idempotency_key": "jobs-key-8c"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "conflicting_variant_requirements"
    assert r.json()["error"]["detail"][0]["row_id"] == bad["rows"][0]["id"]
    empty = _draft(api, pid, png, "direct_transform", "draft-key-8d", requested_variants=3)
    r = api.raw("POST", f"{P}/{pid}/variant-drafts/{empty['id']}:create-jobs",
                json={"expected_revision": empty["revision"], "idempotency_key": "jobs-key-8d"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "empty_plan"
    _no_work(api, pid)


def test_vd11_second_new_family_draft_for_same_source_conflicts(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    src = _png_src(api, pid)
    row = [{"label": "S", "raster_transform": RESIZE}]
    d1 = _draft(api, pid, src, "direct_transform", "draft-key-11a", rows=row)
    d2 = _draft(api, pid, src, "direct_transform", "draft-key-11b", rows=row)
    _create(api, pid, d1, "jobs-key-11a")
    r = api.raw("POST", f"{P}/{pid}/variant-drafts/{d2['id']}:create-jobs",
                json={"expected_revision": d2["revision"], "idempotency_key": "jobs-key-11b"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "source_family_changed"
    assert len(list_families(api.studio.registry.get(pid).store)) == 1
    assert len(api.get(f"{V2}/{pid}/jobs")["jobs"]) == 1


def test_idempotency_replay_and_conflict(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    src = _png_src(api, pid)
    body = {"asset_id": src["asset_id"], "version_id": src["version_id"], "method": "direct_transform",
            "rows": [{"label": "S", "raster_transform": RESIZE}], "idempotency_key": "draft-key-i"}
    d = api.post(f"{P}/{pid}/variant-drafts", body)
    assert api.post(f"{P}/{pid}/variant-drafts", body)["id"] == d["id"]
    r = api.raw("POST", f"{P}/{pid}/variant-drafts", json={**body, "requested_variants": 2})
    assert r.status_code == 409
    first = _create(api, pid, d, "jobs-key-i")
    assert _create(api, pid, d, "jobs-key-i") == first
    r = api.raw("POST", f"{P}/{pid}/variant-drafts/{d['id']}:create-jobs",
                json={"expected_revision": d["revision"] + 5, "idempotency_key": "jobs-key-i"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_conflict"
    assert len(api.get(f"{V2}/{pid}/jobs")["jobs"]) == 1 and len(list_families(api.studio.registry.get(pid).store)) == 1


def test_prepare_references_glb_and_alpha_png(make_api) -> None:
    api = make_api(coordinator=False)
    pid = new_project(api)
    glb = _import(api, pid, "Crate", _glb(), "crate.glb")
    d = _draft(api, pid, glb, "image_edit_reconstruct", "draft-key-r1", change_request="x")
    refs = api.post(f"{P}/{pid}/variant-drafts/{d['id']}:prepare-references")
    assert [i["view"] for i in refs["images"]] == ["three_quarter", "rear", "side"]
    assert refs["primary_view"] == "three_quarter" and [i["role"] for i in refs["images"]][0] == "primary"
    ctx = api.studio.registry.get(pid)
    for i in refs["images"]:
        art = ctx.store.artifact(i["artifact_id"])
        assert art.role == "source_render" and art.lineage == [d["source"]["artifacts"][0]["artifact_id"]]
        assert art.meta["renderer"] and ctx.store.artifact_bytes(art.id)[:4] == b"\x89PNG"
    again = api.post(f"{P}/{pid}/variant-drafts/{d['id']}:prepare-references")
    assert again["id"] == refs["id"] and again["images"] == refs["images"]
    assert _get(api, pid, d["id"])["reference_set_id"] == refs["id"]
    im = Image.new("RGBA", (16, 16), (255, 0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    cut = _import(api, pid, "Cutout", buf.getvalue(), "cut.png")
    d2 = _draft(api, pid, cut, "image_edit", "draft-key-r2", change_request="x")
    r2 = api.post(f"{P}/{pid}/variant-drafts/{d2['id']}:prepare-references")
    (img,) = r2["images"]
    assert img["view"] == "image" and img["role"] == "primary" and img["params"]["alpha_composited"] is True
    assert img["params"]["background"] == [200, 200, 200]
    art = ctx.store.artifact(img["artifact_id"])
    assert art.role == "source_prepared"
    px = Image.open(io.BytesIO(ctx.store.artifact_bytes(art.id)))
    assert px.mode == "RGB" and px.getpixel((3, 3)) == (200, 200, 200)


def _styled_source(ctx: Any, name: str, color: int) -> dict[str, str]:
    cfg, _ = ctx.config()
    from assetstudio_core.inheritance import build_snapshot

    sha = ctx.store.save_snapshot(build_snapshot(cfg, "concept", {"kind": Kind.concept_art}))
    art = ctx.store.register_artifact(png_bytes(color=(color, 5, 5)), "image", "image/png")
    r = publish(ctx.store, PublishRequest(
        op_id=f"op-{name}", idempotency_key=f"op-{name}", artifacts={"image": art.id}, preview_role=None,
        origin=Origin.generated, details={"config_snapshot_sha": sha},
        new_asset=NewAsset(name.lower(), name, Kind.concept_art, Origin.generated, "concept")))
    ctx.index.upsert(ctx.store.get(manifest_key(r.asset_id), AssetManifest)[0])
    return {"asset_id": r.asset_id, "version_id": r.version_id}


def test_style_conflict_blocks_generative_only(make_api) -> None:
    api = make_api(coordinator=False)
    pid = new_project(api)
    cfg = api.get(f"{P}/{pid}/config")["config"]
    cfg["styles"] = {"warm": {"label": "Warm", "guide": "warm light"}}
    cfg["categories"] = [{"id": "concept", "slug": "concept", "label": "Concept", "defaults": {
        "kind": "concept_art", "style": {"mode": "value", "value": "warm"}}}]
    assert api.raw("PATCH", f"{P}/{pid}/config", json={"expected_revision": 1, "config": cfg}).status_code == 200
    ctx = api.studio.registry.get(pid)
    gen, direct = _styled_source(ctx, "Gen", 10), _styled_source(ctx, "Direct", 20)
    cfg["styles"]["warm"]["guide"] = "cold light"
    assert api.raw("PATCH", f"{P}/{pid}/config", json={"expected_revision": 2, "config": cfg}).status_code == 200
    caps = api.get(f"{P}/{pid}/assets/{gen['asset_id']}/versions/{gen['version_id']}/variant-capabilities")
    assert caps["style"]["conflict"] and caps["style"]["current_sha"] != caps["style"]["source_sha"]
    d = _prepare(api, pid, _draft(api, pid, gen, "image_edit", "draft-key-s1", change_request="x"))
    r = api.raw("POST", f"{P}/{pid}/variant-drafts/{d['id']}:create-jobs",
                json={"expected_revision": d["revision"], "idempotency_key": "jobs-key-s1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "style_source_conflict"
    assert "earlier project style" in r.json()["error"]["message"]
    acked = api.raw("PATCH", f"{P}/{pid}/variant-drafts/{d['id']}", json={
        "expected_revision": d["revision"], "style_ack": True}).json()
    out = _create(api, pid, acked, "jobs-key-s2")
    ctx_plan = json.loads(ctx.store.repo.read_object(f"variant-plans/{out['plan_id']}.json").data)
    assert ctx_plan["style"]["conflict"] and ctx_plan["style"]["acknowledged"]
    assert ctx_plan["style"]["application"] == "current_project_style"
    dd = _draft(api, pid, direct, "direct_transform", "draft-key-s3", rows=[{"label": "S", "raster_transform": RESIZE}])
    out2 = _create(api, pid, dd, "jobs-key-s3")
    plan2 = json.loads(ctx.store.repo.read_object(f"variant-plans/{out2['plan_id']}.json").data)
    assert plan2["style"]["conflict"] and plan2["style"]["application"] == "source_preserved"
