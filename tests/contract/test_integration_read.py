"""Integration read API: immutable projection, exact versions, resolve idempotency, verified content (INT-SPEC §5.2)."""
from __future__ import annotations

import hashlib
import io
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import trimesh
from assetstudio_core.delivery import parse_descriptor, parse_manifest
from fastapi.testclient import TestClient

from tests.conftest import Api, new_project, png_bytes
from tests.integration_support import client, integration_app_for, make_token

V1 = "/api/integration/v1"
MISSING_VER = "ver_0000000000000000"


def _glb(extents: tuple[float, float, float] = (1, 1, 1), shift: tuple[float, float, float] = (0, 0, 0)) -> bytes:
    mesh = trimesh.creation.box(extents=extents)
    mesh.apply_translation(shift)
    buf = io.BytesIO()
    mesh.export(buf, file_type="glb")
    return buf.getvalue()


def _import(api: Api, pid: str, name: str, data: bytes, filename: str, **commit: Any) -> dict:
    prev = api.post(f"/api/v1/projects/{pid}/imports:preview", files={"file": (filename, data)})
    assert prev["ok"], prev
    return api.post(f"/api/v1/projects/{pid}/imports:commit", {
        "import_id": prev["import_id"], "name": name, "kind": prev["suggested_kind"],
        "idempotency_key": f"imp-{name}-{len(data)}", **commit})


@dataclass
class Env:
    api: Api
    lib: str
    c: TestClient
    server_id: str
    app: Any
    root: Path

    def ref(self, imp: dict, **over: str) -> dict[str, str]:
        return {"server_id": self.server_id, "library_id": self.lib, "asset_id": imp["asset_id"],
                "version_id": imp["version_id"], **over}

    def resolve(self, refs: list[dict], reps: list[str] | None = None) -> list[dict]:
        body: dict[str, Any] = {"refs": refs}
        if reps:
            body["target"] = {"representations": reps}
        r = self.c.post(f"{V1}/libraries/{self.lib}/resolve", json=body)
        assert r.status_code == 200, r.text
        return r.json()["entries"]

    def url(self, path: str) -> str:
        return f"{V1}/libraries/{self.lib}{path}"

    def tree(self, *tops: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for top in tops:
            for p in sorted((self.root / top).rglob("*")) if (self.root / top).exists() else []:
                if p.is_file():
                    out[str(p.relative_to(self.root))] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out


@pytest.fixture
def env(make_api) -> Env:
    api = make_api(engine="none", coordinator=False)
    lib = new_project(api)
    app, fastapi_app = integration_app_for(api)
    token = make_token(app, "godot", ["assets:read"], [lib])
    return Env(api, lib, client(app, token), fastapi_app.state.identity.server_id, app,
               api.studio.registry.get(lib).root)


def test_list_filters_paging_and_reset(env: Env) -> None:
    a = _import(env.api, env.lib, "Alpha", _glb(), "a.glb", tags=["tree", "big"])
    _import(env.api, env.lib, "Beta", _glb((2, 1, 1)), "b.glb", tags=["tree"])
    _import(env.api, env.lib, "Tile", png_bytes(), "t.png")
    r = env.c.get(env.url("/assets"))
    items = r.json()["items"]
    assert [i["display_name"] for i in items] == ["Alpha", "Beta"]  # PNG asset is invisible
    assert items[0]["asset_id"] == a["asset_id"] and items[0]["current_version_id"] == a["version_id"]
    assert {"category_id", "tags", "display_version", "metadata_revision", "has_thumbnail"} <= items[0].keys()
    assert [i["display_name"] for i in env.c.get(env.url("/assets"), params={"tags": "tree,big"}).json()["items"]] \
        == ["Alpha"]
    assert env.c.get(env.url("/assets"), params={"q": "beta"}).json()["items"][0]["display_name"] == "Beta"
    assert env.c.get(env.url("/assets"), params={"kind": "sprite"}).status_code == 422
    assert env.c.get(env.url("/assets"), params={"limit": 0}).status_code == 400
    first = env.c.get(env.url("/assets"), params={"limit": 1}).json()
    assert len(first["items"]) == 1 and first["next_cursor"]
    second = env.c.get(env.url("/assets"), params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert second["items"][0]["display_name"] == "Beta" and second["next_cursor"] is None
    stale = env.c.get(env.url("/assets"), params={"limit": 1, "cursor": first["next_cursor"], "q": "x"}).json()
    assert stale == {"items": [], "next_cursor": None, "reset_required": True}
    _import(env.api, env.lib, "Gamma", _glb((3, 1, 1)), "c.glb")  # index changed under the cursor
    reset = env.c.get(env.url("/assets"), params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert reset["reset_required"] is True and reset["items"] == []


def test_detail_version_and_resolve_flow(env: Env) -> None:
    imp = _import(env.api, env.lib, "Crate", _glb((2, 1, 0.5), (1.3, 0.2, -0.7)), "crate.glb")
    aid, vid = imp["asset_id"], imp["version_id"]
    det = env.c.get(env.url(f"/assets/{aid}")).json()
    assert det["current_version_id"] == vid and det["versions"][0]["version_id"] == vid
    assert det["library_id"] == env.lib and det["display_name"] == "Crate"
    ver = env.c.get(env.url(f"/assets/{aid}/versions/{vid}")).json()
    assert ver["descriptor"] == {"state": "not_prepared", "sha256": None, "json": None}
    assert ver["deliveries"] == [] and ver["is_current"] is True and ver["source_available"] is False
    assert env.c.get(env.url(f"/assets/{aid}/versions/{vid}/descriptor")).json()["error"]["code"] == \
        "delivery_preparing"
    before = env.tree("versions", "manifests")
    assert before
    entry = env.resolve([env.ref(imp)])[0]
    assert entry["state"] == "ready" and entry["error"] is None and entry["dependencies"] == []
    (dsum,) = entry["deliveries"]
    assert dsum["representation"] == "portable_glb_v1" and dsum["budget"]["within_ipad_budget"] is True
    raw = env.c.get(env.url(f"/assets/{aid}/versions/{vid}/descriptor"))
    assert raw.status_code == 200 and raw.headers["content-type"] == "application/json"
    sha = hashlib.sha256(raw.content).hexdigest()
    assert sha == entry["descriptor_sha256"] == raw.headers["x-content-sha256"] and raw.headers["etag"] == f'"{sha}"'
    assert raw.content.decode() == entry["descriptor_json"]
    d = parse_descriptor(raw.content)
    assert d.forward_axis == "+Z" and d.placement_anchor == _anchor(d) and d.footprint_radius_m
    _check_bounds(d)
    assert d.material_slots and d.preview_warnings == ["legacy_projection"]
    man = env.c.get(env.url(f"/deliveries/{dsum['delivery_id']}/manifest"))
    assert hashlib.sha256(man.content).hexdigest() == dsum["manifest_sha256"] == man.headers["x-content-sha256"]
    m = parse_manifest(man.content)
    assert m.descriptor_sha256 == sha and m.entrypoint == "model.glb" and m.dependencies == []
    ver2 = env.c.get(env.url(f"/assets/{aid}/versions/{vid}")).json()
    assert ver2["descriptor"]["state"] == "ready" and ver2["descriptor"]["json"] == entry["descriptor_json"]
    assert ver2["deliveries"] == [dsum] and ver2["asset_key"] == m.asset_ref.key()
    files_before = env.tree("descriptors", "deliveries", "delivery_index", "delivery_artifacts")
    assert env.resolve([env.ref(imp)]) == [entry]  # idempotent: identical entry, no new records
    assert env.tree("descriptors", "deliveries", "delivery_index", "delivery_artifacts") == files_before
    assert env.tree("versions", "manifests") == before  # history is never rewritten
    _check_content(env, m)


def _anchor(d: Any) -> tuple[str, str, str]:
    lo, hi = d.bounds_min, d.bounds_max
    got = tuple(float(v) for v in d.placement_anchor)
    assert got[1] == pytest.approx(float(lo[1]), abs=2e-6)  # bottom-center: y of the lowest vertex
    assert got[0] == pytest.approx((float(lo[0]) + float(hi[0])) / 2, abs=2e-6)
    assert got[2] == pytest.approx((float(lo[2]) + float(hi[2])) / 2, abs=2e-6)
    return d.placement_anchor


def _check_bounds(d: Any) -> None:
    lo, hi = [float(v) for v in d.bounds_min], [float(v) for v in d.bounds_max]
    exp_lo, exp_hi = np.array([0.3, -0.3, -0.95], np.float32), np.array([2.3, 0.7, -0.45], np.float32)
    for i in range(3):  # quantized box must enclose the float32 geometry, at most 1 micrometre loose
        assert lo[i] <= exp_lo[i] and lo[i] > exp_lo[i] - 2e-6
        assert hi[i] >= exp_hi[i] and hi[i] < exp_hi[i] + 2e-6
    assert float(d.footprint_radius_m) >= (1.0 ** 2 + 0.25 ** 2) ** 0.5 - 1e-5  # half-extents 1.0 x 0.25


def _check_content(env: Env, m: Any) -> None:
    f = m.files[0]
    full = env.c.get(env.url(f"/artifacts/{f.artifact_id}/content"))
    assert full.status_code == 200 and hashlib.sha256(full.content).hexdigest() == f.sha256
    assert full.headers["x-content-sha256"] == f.sha256 and full.headers["etag"] == f'"{f.sha256}"'
    assert full.headers["x-content-type-options"] == "nosniff" and "immutable" in full.headers["cache-control"]
    part = env.c.get(env.url(f"/artifacts/{f.artifact_id}/content"), headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and len(part.content) == 10 and part.content == full.content[:10]


def test_resolve_per_entry_states_and_isolation(env: Env) -> None:
    good = _import(env.api, env.lib, "Good", _glb(), "g.glb")
    other_lib = new_project(env.api, "Other")
    tile = _import(env.api, env.lib, "Tile", png_bytes(), "t.png")
    refs = [env.ref(good), env.ref(good, server_id="00000000-0000-4000-8000-000000000000"),
            env.ref(good, library_id=other_lib), env.ref(good, version_id=MISSING_VER),
            env.ref(good, asset_id="ast_0000000000000000"), env.ref(tile)]
    states = env.resolve(refs)
    assert [e["state"] for e in states] == ["ready", "server_identity_mismatch", "forbidden", "not_found",
                                            "not_found", "not_found"]
    assert [e["error"]["code"] if e["error"] else None for e in states] == [
        None, "server_identity_mismatch", "forbidden", "version_unavailable", "asset_not_found", "asset_not_found"]
    only_source = env.resolve([env.ref(good)], ["godot_static_source_v1"])[0]
    assert only_source["state"] == "unsupported" and only_source["error"]["code"] == "unsupported_representation"
    assert env.c.post(env.url("/resolve"), json={"refs": []}).status_code == 400
    r = env.c.get(env.url(f"/assets/{good['asset_id']}/versions/{MISSING_VER}"))
    assert r.status_code == 404 and r.json()["error"]["code"] == "version_unavailable"
    assert env.c.get(env.url(f"/assets/{tile['asset_id']}")).status_code == 404
    assert env.c.get(env.url(f"/assets/{good['asset_id']}/versions/{good['version_id']}/thumbnail")).status_code in (
        200, 404)


def test_thumbnail_serves_preview_artifact(env: Env) -> None:
    imp = _import(env.api, env.lib, "Thumb", _glb(), "t.glb")
    ver = env.c.get(env.url(f"/assets/{imp['asset_id']}/versions/{imp['version_id']}")).json()
    assert ver["has_thumbnail"] is True
    r = env.c.get(env.url(f"/assets/{imp['asset_id']}/versions/{imp['version_id']}/thumbnail"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/")
    assert hashlib.sha256(r.content).hexdigest() == r.headers["x-content-sha256"]


def test_slot_slugs_and_sanitized_metadata() -> None:
    from assetstudio_server.services.delivery_projection import material_slots, sanitize

    prim = {"attributes": {"POSITION": 0}}
    doc = {"materials": [{"name": "Bark Rough!"}, {"name": "bark rough?"}, {"name": "__"}, {}],
           "meshes": [{"primitives": [{**prim, "material": 0}, {**prim, "material": 1}, {**prim, "material": 2},
                                      {**prim, "material": 3}, prim]}]}
    ids = [m.slot_id for m in material_slots(doc)]
    assert ids == ["bark_rough_", "bark_rough__2", "material_2", "material_3", "default"]
    out = sanitize({"a": 0.5, "b": [1, float("nan"), object()], "c": {"d": None}})
    assert out == {"a": "0.5", "b": [1, None, None], "c": {"d": None}}


def test_content_requires_delivery_marker_and_verifies_blobs(env: Env) -> None:
    done = _import(env.api, env.lib, "Done", _glb(), "d.glb")
    fresh = _import(env.api, env.lib, "Fresh", _glb((2, 2, 2)), "f.glb")
    env.resolve([env.ref(done)])
    m = parse_manifest(env.c.get(env.url(f"/deliveries/{_delivery_id(env, done)}/manifest")).content)
    fresh_art = env.api.get(f"/api/v1/projects/{env.lib}/assets/{fresh['asset_id']}")["files"][0]["artifact_id"]
    unknown = env.c.get(env.url(f"/artifacts/{fresh_art}/content"))
    ghost = env.c.get(env.url("/artifacts/art_0000000000000000/content"))
    assert unknown.status_code == ghost.status_code == 404 and unknown.json() == ghost.json()
    assert env.c.get(env.url("/deliveries/dlv_0000000000000000/manifest")).status_code == 404
    f = m.files[0]
    store = env.api.studio.registry.get(env.lib).store
    path = store.repo.blob_path(f.sha256)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    original = path.read_bytes()
    path.write_bytes(b"\x00" * len(original))
    bad = env.c.get(env.url(f"/artifacts/{f.artifact_id}/content"))
    assert bad.status_code >= 400 and bad.content != original and b"\x00" * 16 not in bad.content
    assert bad.json()["error"]["code"] == "integrity_mismatch"


def _delivery_id(env: Env, imp: dict) -> str:
    ver = env.c.get(env.url(f"/assets/{imp['asset_id']}/versions/{imp['version_id']}")).json()
    return ver["deliveries"][0]["delivery_id"]


def test_scope_and_library_grants(env: Env) -> None:
    other = new_project(env.api, "Other")
    token = make_token(env.app, "narrow", ["assets:read"], [other])
    narrow = client(env.app, token)
    r = narrow.get(env.url("/assets"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    r = narrow.post(env.url("/resolve"), json={"refs": [{"server_id": env.server_id, "library_id": env.lib,
                                                         "asset_id": "ast_0000000000000000",
                                                         "version_id": MISSING_VER}]})
    assert r.status_code == 403
    publish_only = client(env.app, make_token(env.app, "pub", ["assets:publish"], [env.lib]))
    assert publish_only.get(env.url("/assets")).status_code == 403
    assert client(env.app).get(env.url("/assets")).status_code == 401
    assert env.c.get(f"{V1}/libraries/prj_zzzzzzzzzzzzzzzz/assets").status_code == 403
