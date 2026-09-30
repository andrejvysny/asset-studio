"""MCP config, library and media tools over the real listener stack (SIMULATED engine, no GPU)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from mcp import ClientSession

from tests.conftest import Api, new_project, png_bytes
from tests.mcp_support import call, call_error, http_client, mcp_app_for, mcp_session

CAT = {"label": "Concept", "slug": "concept", "defaults": {"kind": {"mode": "value", "value": "concept_art"}}}


def run(api: Api, body: Callable[[ClientSession, Any], Awaitable[None]], scope: str = "full") -> None:
    app = mcp_app_for(api.c.app)
    token = app.deps.tokens.create("claude", scope)

    async def main() -> None:
        async with mcp_session(app, token) as s:
            await body(s, app)
    asyncio.run(main())


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_style_roundtrip_merge_and_errors(api: Api) -> None:
    pid = new_project(api)

    async def body(s: ClientSession, app: Any) -> None:
        got = await call(s, "config_get", section="styles")
        assert got["value"] == {} and got["revision"] == 1
        out = await call(s, "config_set", section="styles", key="warm", value={"label": "Warm", "guide": "warm"},
                         project_id=pid)
        assert out["revision"] == 2 and out["value"]["guide"] == "warm"
        out = await call(s, "config_set", section="styles", key="warm", value={"negative": "cold"}, merge=True)
        assert out["revision"] == 3 and out["value"]["label"] == "Warm" and out["value"]["negative"] == "cold"
        assert (await call(s, "config_get", section="styles"))["value"]["warm"]["guide"] == "warm"
        assert "yaml" in await call(s, "config_get", include_yaml=True)
        assert "stale" in await call_error(s, "config_set", section="styles", key="x", value={}, expected_revision=1)
        assert "invalid_config" in await call_error(s, "config_set", section="styles", key="x",
                                                    value={"palette": [{"hex": "red"}]})
        assert "requires `key`" in await call_error(s, "config_set", section="styles", value={})
        assert (await call(s, "config_get"))["revision"] == 3  # failed edits saved nothing
        deleted = await call(s, "config_delete", section="styles", key="warm", expected_revision=3)
        assert deleted["revision"] == 4
        assert (await call(s, "config_get", section="styles"))["value"] == {}
        assert "unknown" in await call_error(s, "config_delete", section="styles", key="warm")
    run(api, body)


def test_style_history_and_effects_tools(api: Api) -> None:
    new_project(api)

    async def body(s: ClientSession, app: Any) -> None:
        await call(s, "config_set", section="styles", key="warm", value={"guide": "v1"})
        await call(s, "config_set", section="styles", key="warm", value={"guide": "v2"}, merge=True)
        await call(s, "config_set", section="defaults", value={"kind": {"mode": "value", "value": "concept_art"},
                                                               "style": {"mode": "value", "value": "warm"}})
        hist = (await call(s, "style_history", style_id="warm"))["revisions"]
        assert [r["content"]["guide"] for r in hist] == ["v2", "v1"] and hist[0]["actor"] == "agent:claude"
        fx = (await call(s, "config_effects"))["effects"]
        assert next(e for e in fx if e["field"] == "style.guide")["classification"] == "conditioning_only"
        assert "both job_id and item_id" in await call_error(s, "config_effects", job_id="job_x")
    run(api, body)


def test_category_upsert_effective_and_pipelines(api: Api) -> None:
    new_project(api)

    async def body(s: ClientSession, app: Any) -> None:
        out = await call(s, "config_set", section="categories", key="concept", value=CAT)
        assert out["value"]["id"] == "concept"
        assert "must equal key" in await call_error(s, "config_set", section="categories", key="a",
                                                    value={**CAT, "id": "b"})
        await call(s, "config_set", section="categories", key="concept", value={"label": "Concept art"}, merge=True)
        cats = (await call(s, "config_get", section="categories"))["value"]
        assert [c["label"] for c in cats] == ["Concept art"]
        eff = await call(s, "config_effective", category_id="concept")
        assert eff["effective"]["kind"]["value"] == "concept_art"
        assert "unknown category" in await call_error(s, "config_effective", category_id="nope")
        pipe = await call(s, "config_set", section="pipelines", key="concept.default",
                          value={"parameters": {"steps": 12}})
        assert pipe["value"]["parameters"] == {"steps": 12}
        assert "invalid_config" in await call_error(s, "config_set", section="pipelines", key="concept.default",
                                                    value={"parameters": {"steps": 9999}})
        ok = await call(s, "config_validate", config=(await call(s, "config_get"))["config"])
        assert ok["ok"] is True
        bad = await call(s, "config_validate", yaml="not: [valid")
        assert bad["ok"] is False
        assert "exactly one" in await call_error(s, "config_validate")
        ret = await call(s, "config_set", section="retention", value={"full_logs": {"keep": False}})
        assert ret["value"]["full_logs"]["keep"] is False
        rev = (await call(s, "config_get"))["revision"]
        replaced = await call(s, "config_replace_yaml", expected_revision=rev,
                              yaml=(await call(s, "config_get", include_yaml=True))["yaml"])
        assert replaced["revision"] == rev + 1
        await call(s, "config_delete", section="categories", key="concept")
    run(api, body)


def test_read_token_cannot_edit_config(api: Api) -> None:
    new_project(api)

    async def body(s: ClientSession, app: Any) -> None:
        assert (await call(s, "config_get"))["revision"] == 1
        assert "forbidden" in await call_error(s, "config_set", section="styles", key="a", value={})
        assert "forbidden" in await call_error(s, "add_media", upload_id="upl_" + "0" * 24)
    run(api, body, scope="read")


def test_media_flow_with_spool_and_image_block(api: Api) -> None:
    pid = new_project(api)

    async def body(s: ClientSession, app: Any) -> None:
        up = await call(s, "upload_file", filename="ref.png", data_base64=b64(png_bytes()))
        added = await call(s, "add_media", upload_id=up["upload_id"], tags=["Moody", "fog"], name="Fog ref")
        assert added["duplicate"] is False and added["item"]["tags"] == ["moody", "fog"]
        found = await call(s, "search_media", tag="fog", q="fog")
        assert [i["id"] for i in found["items"]] == [added["item"]["id"]]
        again = await call(s, "add_media", upload_id=up["upload_id"])
        assert again["duplicate"] is True
        upd = await call(s, "update_media", media_id=added["item"]["id"], note="n")
        assert upd["note"] == "n"
        art = added["item"]["artifact_id"]
        meta_only = await call(s, "get_artifact", artifact_id=art)
        assert meta_only["mime"] == "image/png"
        res = await s.call_tool("get_artifact", {"artifact_id": art, "include_image": True})
        assert not res.isError and [c.type for c in res.content] == ["text", "image"]
        assert "image" in await call_error(s, "add_media", upload_id=(await call(
            s, "upload_file", filename="x.png", data_base64=b64(b"notanimage")))["upload_id"])
        await call(s, "archive_media", media_id=added["item"]["id"])
        assert (await call(s, "search_media"))["items"] == []
        assert len((await call(s, "search_media", archived=True))["items"]) == 1
        await call(s, "archive_media", media_id=added["item"]["id"], restore=True)
        assert len((await call(s, "search_media"))["items"]) == 1
    run(api, body)
    assert pid


def test_upload_url_put_once_and_download_url(api: Api) -> None:
    pid = new_project(api)
    data = png_bytes(32, 32, color=(1, 2, 3))

    async def body(s: ClientSession, app: Any) -> None:
        target = await call(s, "create_upload_url", filename="big.png")
        path = "/files/up/" + target["url"].rsplit("/files/up/", 1)[1]
        async with http_client(app, None) as http:
            r = await http.put(path, content=data)
            assert r.status_code == 200 and r.json()["upload_id"] == target["upload_id"]
            assert (await http.put(path, content=data)).status_code == 409
            assert (await http.put(path[:-2] + "xx", content=data)).status_code == 403
            added = await call(s, "add_media", upload_id=target["upload_id"])
            art = added["item"]["artifact_id"]
            meta = await call(s, "get_artifact", artifact_id=art, project_id=pid)
            dl = await call(s, "get_download_url", artifact_id=art)
            r = await http.get("/files/down/" + dl["url"].rsplit("/files/down/", 1)[1])
            assert r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == meta["sha256"]
    run(api, body)


def test_import_asset_and_family_and_asset_update(api: Api) -> None:
    new_project(api)

    async def body(s: ClientSession, app: Any) -> None:
        await call(s, "config_set", section="categories", key="concept", value=CAT)
        up = await call(s, "upload_file", filename="tavern.png", data_base64=b64(png_bytes()))
        dry = await call(s, "import_asset", upload_ids=[up["upload_id"]], name="Tavern", dry_run=True)
        assert dry["preview"]["ok"] and "committed" not in dry
        assert (await call(s, "search_assets", q="Tavern"))["items"] == []
        out = await call(s, "import_asset", upload_ids=[up["upload_id"]], name="Tavern", category_id="concept",
                         tags=["pub"], idempotency_key="imp-key-0001")
        asset_id = out["committed"]["asset_id"]
        found = await call(s, "search_assets", q="Tavern", category_id="concept")
        assert [i["asset_id"] for i in found["items"]] == [asset_id]
        asset = await call(s, "get_asset", asset_id=asset_id)
        assert asset["version_history"]["current_version_id"] == out["committed"]["version_id"]
        upd = await call(s, "update_asset", asset_id=asset_id, display_name="Tavern 2", tags=["x"])
        assert upd["display_name"] == "Tavern 2"
        again = await call(s, "set_current_version", asset_id=asset_id, version_id=out["committed"]["version_id"])
        assert again["current_version_id"] == out["committed"]["version_id"]
        assert len((await call(s, "list_categories"))["categories"]) == 1
        assert "families" in await call(s, "list_families")
        grouped = await call(s, "search_assets", group_by="family")
        assert "groups" in grouped
        mat = await call(s, "upload_file", filename="m.png", data_base64=b64(png_bytes(8, 8, color=(9, 9, 9))))
        prev = await call(s, "import_asset", upload_ids=[mat["upload_id"]], name="Mat", mode="material",
                          dry_run=True)
        assert prev["preview"]["format"] == "material_bundle"
    run(api, body)


def test_shot_list_import_then_get_and_put(api: Api) -> None:
    new_project(api)

    async def body(s: ClientSession, app: Any) -> None:
        await call(s, "config_set", section="categories", key="concept", value=CAT)
        csv = "name,category,brief\nDragon,concept,a dragon\nKnight,concept,a knight\n"
        dry = await call(s, "shot_list_import", text=csv, dry_run=True)
        assert [r["default_action"] for r in dry["preview"]["rows"]] == ["create", "create"]
        assert (await call(s, "shot_list_get"))["items"] == []
        out = await call(s, "shot_list_import", text=csv)
        assert out["committed"]["created"] == 2
        got = await call(s, "shot_list_get")
        assert sorted(i["name"] for i in got["items"]) == ["Dragon", "Knight"] and got["revision"] == out["committed"]["revision"]
        keep = [{"id": i["id"], "name": i["name"], "category_id": "concept"} for i in got["items"][:1]]
        put = await call(s, "shot_list_put", items=keep, expected_revision=got["revision"])
        assert len(put["items"]) == 1
        assert "stale" in await call_error(s, "shot_list_put", items=keep, expected_revision=1)
    run(api, body)
