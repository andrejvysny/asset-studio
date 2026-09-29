"""Library contracts: empty generic project, imports, publication, versions, safety. (G01 I01 I02 I04 P02 S04)"""
from __future__ import annotations

import io
import json
import struct

import trimesh

from tests.conftest import Api, new_project, png_bytes


def _glb(textured: bool = False) -> bytes:
    buf = io.BytesIO()
    trimesh.creation.box().export(buf, file_type="glb")
    return buf.getvalue()


def _glb_with_external_uri() -> bytes:
    doc = json.dumps({"asset": {"version": "2.0"}, "buffers": [{"uri": "http://example.com/x.bin",
                                                                  "byteLength": 4}]}).encode()
    doc += b" " * (-len(doc) % 4)
    body = struct.pack("<I4s", len(doc), b"JSON") + doc
    return struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body


def _import(api: Api, pid: str, name: str, data: bytes, filename: str, **commit: object) -> dict:
    prev = api.post(f"/api/v1/projects/{pid}/imports:preview", files={"file": (filename, data)})
    assert prev["ok"], prev
    return api.post(f"/api/v1/projects/{pid}/imports:commit", {
        "import_id": prev["import_id"], "name": name, "kind": prev["suggested_kind"],
        "idempotency_key": f"imp-{name}-{len(data)}", **commit})


def test_empty_project_is_generic(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    cfg = api.get(f"/api/v1/projects/{pid}/config")
    text = cfg["yaml"].lower()
    for word in ("biome", "forest", "hand-painted", "hand painted", "fantasy", "godot"):
        assert word not in text
    assert cfg["config"]["categories"] == [] and cfg["config"]["styles"] == {}
    lib = api.get(f"/api/v1/projects/{pid}/assets")
    assert lib["total"] == 0 and lib["planned_total"] == 0


def test_import_png_and_glb_without_engine(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    png = _import(api, pid, "Red tile", png_bytes(), "red.png")
    glb = _import(api, pid, "Box", _glb(), "box.glb")
    lib = api.get(f"/api/v1/projects/{pid}/assets")
    assert lib["total"] == 2
    assert {i["origin"] for i in lib["items"]} == {"imported"}
    assert {i["kind"] for i in lib["items"]} == {"concept_art", "model3d"}
    assert api.get(f"/api/v1/projects/{pid}/assets", params={"origin": "imported", "kind": "model3d"})["total"] == 1
    detail = api.get(f"/api/v1/projects/{pid}/assets/{glb['asset_id']}")
    assert detail["shown_version"]["display_version"] == 1
    assert detail["shown_version"]["licence"]["status"] == "unknown"
    art = detail["files"][0]["artifact_id"]
    r = api.c.get(f"/api/v1/projects/{pid}/artifacts/{art}/content")
    assert r.status_code == 200 and r.content[:4] == b"glTF"
    assert png["display_version"] == 1


def test_duplicate_commit_is_idempotent_and_new_version_increments(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    prev = api.post(f"/api/v1/projects/{pid}/imports:preview", files={"file": ("a.png", png_bytes())})
    body = {"import_id": prev["import_id"], "name": "A", "kind": "concept_art", "idempotency_key": "key-aaaa-1"}
    first = api.post(f"/api/v1/projects/{pid}/imports:commit", body)
    again = api.post(f"/api/v1/projects/{pid}/imports:commit", body)
    assert first == again
    prev2 = api.post(f"/api/v1/projects/{pid}/imports:preview", files={"file": ("b.png", png_bytes(color=(1, 2, 3)))})
    v2 = api.post(f"/api/v1/projects/{pid}/imports:commit", {
        "import_id": prev2["import_id"], "name": "A", "kind": "concept_art", "target_asset_id": first["asset_id"],
        "expected_current_version": first["version_id"], "idempotency_key": "key-aaaa-2"})
    assert v2["display_version"] == 2 and v2["asset_id"] == first["asset_id"]
    # stale pointer: the current version moved on
    prev3 = api.post(f"/api/v1/projects/{pid}/imports:preview", files={"file": ("c.png", png_bytes(color=(9, 9, 9)))})
    r = api.raw("POST", f"/api/v1/projects/{pid}/imports:commit", json={
        "import_id": prev3["import_id"], "name": "A", "kind": "concept_art", "target_asset_id": first["asset_id"],
        "expected_current_version": first["version_id"], "idempotency_key": "key-aaaa-3"})
    assert r.status_code == 409
    # set current back to v1 with audit; history immutable
    api.post(f"/api/v1/projects/{pid}/assets/{first['asset_id']}:set-current", {
        "version_id": first["version_id"], "expected_current_version": v2["version_id"],
        "idempotency_key": "setcur-1234"})
    vers = api.get(f"/api/v1/projects/{pid}/assets/{first['asset_id']}/versions")
    assert vers["current_version_id"] == first["version_id"] and len(vers["versions"]) == 2
    assert [p["reason"] for p in vers["pointer_log"]][-1] == "set current"


def test_unsafe_imports_rejected(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    r = api.raw("POST", f"/api/v1/projects/{pid}/imports:preview", files={"file": ("x.glb", _glb_with_external_uri())})
    assert r.status_code == 200 and r.json()["ok"] is False
    r = api.raw("POST", f"/api/v1/projects/{pid}/imports:preview", files={"file": ("x.png", b"\x89PNG garbage")})
    assert r.status_code == 200 and r.json()["ok"] is False
    r = api.raw("POST", f"/api/v1/projects/{pid}/imports:preview", files={"file": ("x.pt", b"pickle")})
    assert r.status_code == 422
    r = api.raw("POST", f"/api/v1/projects/{pid}/imports:preview", files={"file": ("x.gltf", b"{}")})
    assert r.status_code == 422
    # a GLB may never be imported as an image kind
    prev = api.post(f"/api/v1/projects/{pid}/imports:preview", files={"file": ("b.glb", _glb())})
    r = api.raw("POST", f"/api/v1/projects/{pid}/imports:commit", json={
        "import_id": prev["import_id"], "name": "B", "kind": "sprite", "idempotency_key": "key-bbbb-1"})
    assert r.status_code == 422


def test_csrf_header_required(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    r = api.c.post("/api/v1/projects", json={"name": "x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf"
    r = api.c.post("/api/v1/projects", json={"name": "x"}, headers={"x-assetstudio": "1", "origin": "http://evil.test"})
    assert r.status_code == 403


def test_index_rebuild_from_manifests(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    _import(api, pid, "One", png_bytes(), "1.png")
    _import(api, pid, "Two", png_bytes(color=(0, 0, 255)), "2.png")
    ctx = api.studio.registry.get(pid)
    ctx.index._db.execute("DELETE FROM assets")
    assert api.get(f"/api/v1/projects/{pid}/assets")["total"] == 0
    assert api.post(f"/api/v1/projects/{pid}/storage:rebuild-index")["indexed"] == 2
    assert api.get(f"/api/v1/projects/{pid}/assets")["total"] == 2


def test_storage_view_and_sentinel(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    _import(api, pid, "One", png_bytes(), "1.png")
    view = api.get(f"/api/v1/projects/{pid}/storage")
    assert view["stats"]["assets"] == 1 and view["stats"]["blobs"] == 2 and view["s3"]["available"] is False
    t = api.post(f"/api/v1/projects/{pid}/storage:test")
    assert t["ok"]
    ctx = api.studio.registry.get(pid)
    assert ctx.store.repo.list_keys("_control/test")[0] == []


def test_config_patch_revision_and_validation(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    cfg = api.get(f"/api/v1/projects/{pid}/config")
    c = cfg["config"]
    c["categories"] = [{"id": "props", "slug": "props", "label": "Props", "defaults": {"kind": "model3d"}},
                       {"id": "loop", "parent_id": "loop2", "slug": "a", "label": "A"},
                       {"id": "loop2", "parent_id": "loop", "slug": "b", "label": "B"}]
    r = api.raw("PATCH", f"/api/v1/projects/{pid}/config", json={"expected_revision": 1, "config": c})
    assert r.status_code == 422 and any("cycle" in e["message"] for e in r.json()["error"]["detail"])
    c["categories"] = c["categories"][:1]
    out = api.raw("PATCH", f"/api/v1/projects/{pid}/config", json={"expected_revision": 1, "config": c}).json()
    assert out["revision"] == 2 and out["effective"]["props"]["kind"]["value"] == "model3d"
    r = api.raw("PATCH", f"/api/v1/projects/{pid}/config", json={"expected_revision": 1, "config": c})
    assert r.status_code == 409
    dup = api.post(f"/api/v1/projects/{pid}/config:validate", {"yaml": "project: {id: a, name: b}\nproject: {}\n"})
    assert dup["ok"] is False and "duplicate" in dup["errors"][0]["message"]
