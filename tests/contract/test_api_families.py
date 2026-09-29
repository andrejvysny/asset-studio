"""Family read/write contracts over the library API. (Variants milestone, Phase A)"""
from __future__ import annotations

from assetstudio_core.domain import AssetManifest
from assetstudio_core.ids import derived_id
from assetstudio_core.kinds import Kind, Origin
from assetstudio_storage.families import attach_asset, create_family
from assetstudio_storage.project import manifest_key
from assetstudio_storage.publication import NewAsset, PublishRequest, publish

from tests.conftest import Api, new_project, png_bytes
from tests.contract.test_api_library import _import


def _setup(api: Api) -> tuple[str, str, dict[str, str]]:
    pid = new_project(api)
    ids = {n: _import(api, pid, n, png_bytes(color=(i * 40, 9, 9)), f"{n}.png")["asset_id"]
           for i, n in enumerate(["Wall", "Wall red", "Banner", "Lonely"])}
    ctx = api.studio.registry.get(pid)
    fid = derived_id("fam", pid, "op-1")
    ver = ctx.store.get(manifest_key(ids["Wall"]), AssetManifest)[0].current_version_id
    create_family(ctx.store, family_id=fid, name="Stone walls", kind=Kind.concept_art, anchor_asset_id=ids["Wall"],
                  anchor_version_id=ver or "", op_id="op-1")
    for n in ("Wall", "Wall red", "Banner"):
        ctx.index.upsert(attach_asset(ctx.store, asset_id=ids[n], family_id=fid, expected_manifest_revision=None),
                         "Stone walls")
    return pid, fid, ids


def test_grouped_and_filtered_listing(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid, fid, ids = _setup(api)
    base = f"/api/v1/projects/{pid}/assets"
    g = api.get(base, params={"group_by": "family"})
    assert g["group_by"] == "family" and g["matching_asset_count"] == 4 and g["matching_group_count"] == 2
    fam = next(x for x in g["groups"] if x["type"] == "family")
    assert fam["family_id"] == fid and fam["total_member_count"] == 3 and len(fam["member_preview"]) == 3
    assert fam["member_preview"][0]["kind_label"] and "planned" not in g
    solo = next(x for x in g["groups"] if x["type"] == "asset")
    assert solo["asset"]["asset_id"] == ids["Lonely"]
    one = api.get(base, params={"group_by": "family", "q": "banner"})["groups"]
    assert one[0]["matching_count"] == 1 and one[0]["total_member_count"] == 3
    assert one[0]["representative_asset_id"] == ids["Banner"]
    flat = api.get(base, params={"family_id": fid})
    assert flat["total"] == 3 and {i["family_name"] for i in flat["items"]} == {"Stone walls"}
    assert api.get(base, params={"q": "stone walls"})["total"] == 3
    page = api.get(base, params={"group_by": "family", "limit": 1})
    assert page["next_cursor"]
    r = api.raw("GET", base, params={"group_by": "family", "limit": 1, "q": "x", "cursor": page["next_cursor"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_cursor"
    assert api.raw("GET", base, params={"family_id": "bogus"}).status_code == 400


def test_asset_detail_family_fields(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid, fid, ids = _setup(api)
    d = api.get(f"/api/v1/projects/{pid}/assets/{ids['Wall red']}")
    assert d["family_id"] == fid and d["family_name"] == "Stone walls" and d["family"]["id"] == fid
    assert d["derived_from"] is None and d["versions"][0]["derivation"] is None
    lone = api.get(f"/api/v1/projects/{pid}/assets/{ids['Lonely']}")
    assert lone["family"] is None and lone["family_id"] is None


def test_family_endpoints_and_rename(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid, fid, ids = _setup(api)
    base = f"/api/v1/projects/{pid}"
    lst = api.get(f"{base}/families")["families"]
    assert [(f["id"], f["total_member_count"]) for f in lst] == [(fid, 3)]
    assert api.get(f"{base}/families/{fid}")["name"] == "Stone walls"
    r = api.raw("PATCH", f"{base}/families/{fid}", json={"expected_revision": 1, "name": "Castle"})
    assert r.status_code == 200 and r.json()["revision"] == 2 and r.json()["total_member_count"] == 3
    r = api.raw("PATCH", f"{base}/families/{fid}", json={"expected_revision": 1, "name": "Late"})
    assert r.status_code == 409
    assert api.raw("PATCH", f"{base}/families/{fid}", json={"expected_revision": 2, "name": "  "}).status_code == 422
    assert api.get(f"{base}/assets", params={"q": "castle"})["total"] == 3
    assert api.get(f"{base}/assets", params={"q": "stone"})["total"] == 0
    d = api.get(f"{base}/assets/{ids['Banner']}")
    assert d["family_name"] == "Castle"
    assert api.raw("GET", f"{base}/families/{derived_id('fam', 'a', 'b')}").status_code == 404


def test_derived_from_in_detail(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid, fid, ids = _setup(api)
    ctx = api.studio.registry.get(pid)
    src = {"asset_id": ids["Wall"], "version_id": "ver_src", "display_version": 1, "display_name": "Wall"}
    art = ctx.store.register_artifact(b"variant", "image", "image/png")
    res = publish(ctx.store, PublishRequest(
        op_id="op-var", idempotency_key="op-var", artifacts={"image": art.id}, preview_role=None,
        origin=Origin.generated, derivation={"method": "image_edit", "source": src},
        new_asset=NewAsset("wall-blue", "Wall blue", Kind.concept_art, Origin.generated, None, family_id=fid)))
    d = api.get(f"/api/v1/projects/{pid}/assets/{res.asset_id}")
    assert d["family_id"] == fid and d["derived_from"] == {**src, "method": "image_edit"}
    assert d["versions"][0]["derivation"]["method"] == "image_edit"
