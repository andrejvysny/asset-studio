"""Frame-sequence and material-bundle imports (I01-I04 for sheet/vfx/material). No engines involved."""
from __future__ import annotations

import io
import json
import zipfile

from tests.conftest import Api, new_project, png_bytes


def _zip(entries: list[tuple[str, bytes]]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for n, d in entries:
            zf.writestr(n, d)
    return out.getvalue()


def _commit(api: Api, pid: str, prev: dict, kind: str, key: str, **extra: object) -> dict:
    return api.post(f"/api/v1/projects/{pid}/imports:commit", {
        "import_id": prev["import_id"], "name": "Spark", "kind": kind, "idempotency_key": key, **extra})


def test_zip_frame_sequence_to_vfx_atlas(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    frames = [(f"fx/spark_{i}.png", png_bytes(16, 16, color=(i * 20, 0, 0), alpha=True)) for i in (10, 2, 1, 3)]
    prev = api.post(f"/api/v1/projects/{pid}/imports:preview", files={"file": ("spark.zip", _zip(frames))})
    assert prev["ok"] and prev["format"] == "frames"
    assert prev["frames"]["order"] == ["fx/spark_1.png", "fx/spark_2.png", "fx/spark_3.png", "fx/spark_10.png"]
    assert prev["allowed_kinds"] == ["sprite_sheet", "vfx_flipbook"]
    r = api.raw("POST", f"/api/v1/projects/{pid}/imports:commit", json={
        "import_id": prev["import_id"], "name": "Spark", "kind": "concept_art", "idempotency_key": "imp-fx-bad"})
    assert r.status_code == 422
    out = _commit(api, pid, prev, "vfx_flipbook", "imp-fx-1", parameters={"fps": 24, "padding": 0, "pow2": True})
    asset = api.get(f"/api/v1/projects/{pid}/assets/{out['asset_id']}")
    roles = {f["role"]: f for f in asset["files"]}
    assert {"atlas", "meta", "preview", "frame_0000", "frame_0003"} <= roles.keys()
    meta = json.loads(api.raw("GET", f"/api/v1/projects/{pid}/artifacts/{roles['meta']['artifact_id']}/content").content)
    assert meta["fps"] == 24 and meta["size"] == [32, 32] and meta["blend"] == "alpha"
    assert [f["source"] for f in meta["frames"]][-1] == "fx/spark_10.png"
    assert asset["manifest"]["origin"] == "imported" and asset["manifest"]["kind"] == "vfx_flipbook"


def test_multi_png_frames_and_bad_params(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    files = [("files", (f"walk_{i}.png", png_bytes(8, 12, alpha=True))) for i in range(3)]
    prev = api.post(f"/api/v1/projects/{pid}/imports:preview-set?mode=frames", files=files)
    assert prev["ok"] and prev["frames"]["size"] == [8, 12]
    r = api.raw("POST", f"/api/v1/projects/{pid}/imports:commit", json={
        "import_id": prev["import_id"], "name": "Walk", "kind": "sprite_sheet", "idempotency_key": "imp-walk-bad",
        "parameters": {"fps": 0}})
    assert r.status_code == 422 and "fps" in r.text
    out = _commit(api, pid, prev, "sprite_sheet", "imp-walk-1", parameters={"columns": 3, "padding": 1})
    assert out["display_version"] == 1


def test_frame_problems_reported_before_commit(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    files = [("files", ("a.png", png_bytes(8, 8))), ("files", ("b.png", png_bytes(9, 8)))]
    prev = api.post(f"/api/v1/projects/{pid}/imports:preview-set?mode=frames", files=files)
    assert not prev["ok"] and "differ in size" in prev["validation"]["checks"][0]["detail"]
    r = api.raw("POST", f"/api/v1/projects/{pid}/imports:commit", json={
        "import_id": prev["import_id"], "name": "X", "kind": "sprite_sheet", "idempotency_key": "imp-x-bad-1"})
    assert r.status_code == 422
    evil = _zip([("../../etc/x.png", png_bytes())])
    r = api.raw("POST", f"/api/v1/projects/{pid}/imports:preview", files={"file": ("evil.zip", evil)})
    assert r.status_code == 422 and "unsafe path" in r.text


def test_material_bundle_explicit_roles(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    files = [("files", ("brick_albedo.png", png_bytes(32, 32))), ("files", ("brick_normal.png", png_bytes(32, 32))),
             ("files", ("brick_x.png", png_bytes(32, 32)))]
    prev = api.post(f"/api/v1/projects/{pid}/imports:preview-set?mode=material", files=files)
    assert prev["ok"]
    assert [m["suggested_role"] for m in prev["maps"]] == ["base_color", "normal", None]  # suggestions only
    body = {"import_id": prev["import_id"], "name": "Brick", "kind": "material", "idempotency_key": "imp-brick-0"}
    r = api.raw("POST", f"/api/v1/projects/{pid}/imports:commit",
                json={**body, "map_roles": {"brick_albedo.png": "base_color", "brick_normal.png": "normal"}})
    assert r.status_code == 422 and "unmapped" in r.text
    r = api.raw("POST", f"/api/v1/projects/{pid}/imports:commit", json={**body, "idempotency_key": "imp-brick-1",
                "map_roles": {"brick_albedo.png": "normal", "brick_normal.png": "normal", "brick_x.png": "ao"}})
    assert r.status_code == 422 and "once" in r.text
    out = api.post(f"/api/v1/projects/{pid}/imports:commit", {**body, "idempotency_key": "imp-brick-2", "map_roles": {
        "brick_albedo.png": "base_color", "brick_normal.png": "normal", "brick_x.png": "roughness"}})
    asset = api.get(f"/api/v1/projects/{pid}/assets/{out['asset_id']}")
    assert {f["role"] for f in asset["files"]} == {"base_color", "normal", "roughness", "preview"}


def test_material_bundle_size_mismatch_blocked(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    files = [("files", ("m_basecolor.png", png_bytes(32, 32))), ("files", ("m_normal.png", png_bytes(16, 16)))]
    prev = api.post(f"/api/v1/projects/{pid}/imports:preview-set?mode=material", files=files)
    assert not prev["ok"]
    r = api.raw("POST", f"/api/v1/projects/{pid}/imports:commit", json={
        "import_id": prev["import_id"], "name": "M", "kind": "material", "idempotency_key": "imp-m-bad-1",
        "map_roles": {"m_basecolor.png": "base_color", "m_normal.png": "normal"}})
    assert r.status_code == 422
