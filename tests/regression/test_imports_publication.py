"""H08/H09/H17 — RI15, IM01, IM02, IM03, IM04, IM05: replay-safe imports, role contracts, authoritative names."""
from __future__ import annotations

from pathlib import Path

import pytest
from assetstudio_core.kinds import Kind, Origin
from assetstudio_processing import atlas
from assetstudio_server.services import imports as imp
from assetstudio_storage.local import LocalBackend
from assetstudio_storage.project import ProjectStore
from assetstudio_storage.publication import KindMismatch, NameTaken, NewAsset, PublishRequest, publish

from tests.conftest import Api, new_project, png_bytes


def _preview(api: Api, pid: str, name: str = "rock.png", data: bytes | None = None) -> dict:
    return api.post(f"/api/v1/projects/{pid}/imports:preview", files={"file": (name, data or png_bytes())})


def _commit_body(prev: dict, key: str, **kw) -> dict:
    return {"import_id": prev["import_id"], "name": "Rock", "kind": "concept_art", "idempotency_key": key, **kw}


@pytest.mark.parametrize("fail_at", ["publish", "receipt", "record_command", "cleanup"])
def test_import_replay_after_failure_at_every_write(make_api, monkeypatch, fail_at: str) -> None:
    """RI15: a crash after any commit write is retried (same or new key) and yields the SAME asset/version."""
    api: Api = make_api(coordinator=False)
    pid = new_project(api)
    prev = _preview(api, pid)
    real = {"publish": imp.publish, "create_or_same": ProjectStore.create_or_same,
            "record": api.studio.journal.record_command, "rmtree": imp.shutil.rmtree}
    armed = {"on": True}

    def boom(*a, **k):
        raise RuntimeError(f"injected crash at {fail_at}")

    if fail_at == "publish":
        def pub(store, req):
            res = real["publish"](store, req)
            if armed["on"]:
                armed["on"] = False
                boom()
            return res
        monkeypatch.setattr(imp, "publish", pub)
    elif fail_at == "receipt":
        def cos(self, key, rec):
            if key.startswith("imports/") and armed["on"]:
                armed["on"] = False
                boom()
            return real["create_or_same"](self, key, rec)
        monkeypatch.setattr(ProjectStore, "create_or_same", cos)
    elif fail_at == "record_command":
        def rec(*a, **k):
            if armed["on"]:
                armed["on"] = False
                boom()
            return real["record"](*a, **k)
        monkeypatch.setattr(api.studio.journal, "record_command", rec)
    else:
        def rm(*a, **k):
            if armed["on"]:
                armed["on"] = False
                boom()
            return real["rmtree"](*a, **k)
        monkeypatch.setattr(imp.shutil, "rmtree", rm)
    with pytest.raises(RuntimeError):
        api.c.post(f"/api/v1/projects/{pid}/imports:commit", json=_commit_body(prev, "import-key-1"),
                   headers={"x-assetstudio": "1"})
    again = api.post(f"/api/v1/projects/{pid}/imports:commit", _commit_body(prev, "import-key-2"))
    same = api.post(f"/api/v1/projects/{pid}/imports:commit", _commit_body(prev, "import-key-2"))
    assert again == same
    assets = api.get(f"/api/v1/projects/{pid}/assets")
    assert assets["total"] == 1 and assets["items"][0]["asset_id"] == again["asset_id"]
    versions = api.get(f"/api/v1/projects/{pid}/assets/{again['asset_id']}/versions")
    assert len(versions.get("versions", versions)) == 1


def test_committed_import_with_other_settings_conflicts(make_api) -> None:
    api: Api = make_api(coordinator=False)
    pid = new_project(api)
    prev = _preview(api, pid)
    api.post(f"/api/v1/projects/{pid}/imports:commit", _commit_body(prev, "import-a-1"))
    r = api.raw("POST", f"/api/v1/projects/{pid}/imports:commit", json=_commit_body(prev, "import-a-2", name="Other"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_committed"


def test_same_key_in_two_projects_is_independent(make_api) -> None:
    """RI03: identical client keys in different projects never return each other's result."""
    api: Api = make_api(coordinator=False)
    p1, p2 = new_project(api, "One"), new_project(api, "Two")
    r1 = api.post(f"/api/v1/projects/{p1}/imports:commit", _commit_body(_preview(api, p1), "shared-key-1"))
    r2 = api.post(f"/api/v1/projects/{p2}/imports:commit", _commit_body(_preview(api, p2), "shared-key-1"))
    assert r1["asset_id"] != r2["asset_id"]
    r = api.raw("POST", f"/api/v1/projects/{p1}/imports:commit",
                json=_commit_body(_preview(api, p1, "b.png"), "shared-key-1"))
    assert r.status_code == 409  # same scope + key, different request


def test_material_duplicate_names_rejected(make_api) -> None:
    """IM04: two uploads normalizing to one name are rejected instead of silently dropping a map."""
    api: Api = make_api(coordinator=False)
    pid = new_project(api)
    files = [("files", ("a b.png", png_bytes())), ("files", ("a_b.png", png_bytes(color=(1, 1, 1))))]
    r = api.raw("POST", f"/api/v1/projects/{pid}/imports:preview-set?mode=material", files=files)
    assert r.status_code == 422 and r.json()["error"]["code"] == "duplicate_names"


def test_material_role_map_by_upload_id(make_api) -> None:
    api: Api = make_api(coordinator=False)
    pid = new_project(api)
    files = [("files", ("wall_albedo.png", png_bytes())), ("files", ("wall_rough.png", png_bytes(color=(9, 9, 9))))]
    prev = api.post(f"/api/v1/projects/{pid}/imports:preview-set?mode=material", files=files)
    ids = {m["filename"]: m["upload_id"] for m in prev["maps"]}
    out = api.post(f"/api/v1/projects/{pid}/imports:commit", {
        "import_id": prev["import_id"], "name": "Wall", "kind": "material", "idempotency_key": "mat-upl-1",
        "map_roles": {ids["wall_albedo.png"]: "base_color", ids["wall_rough.png"]: "roughness"}})
    v = api.get(f"/api/v1/projects/{pid}/assets/{out['asset_id']}")
    assert v


def test_frame_budget_checked_before_decode(monkeypatch) -> None:
    """IM05: aggregate decoded bytes are checked from headers before any frame is decoded."""
    decoded = []
    monkeypatch.setattr(atlas, "decode_rgba", lambda d: decoded.append(1))
    monkeypatch.setattr(atlas, "MAX_DECODED_BYTES", 64 * 64 * 4 * 3)
    frames = [(f"f{i:02d}.png", png_bytes(64, 64)) for i in range(8)]
    with pytest.raises(atlas.FrameError, match="MiB|decoded"):
        atlas.decode_frames(frames)
    assert decoded == []


def test_publish_rejects_kind_mismatch_and_duplicate_name(tmp_path: Path) -> None:
    """IM01 + IM02: results cannot land on an asset of another kind; names are reserved authoritatively."""
    store = ProjectStore(LocalBackend(tmp_path), "prj_0000000000000000")
    art = store.register_artifact(png_bytes(), "image", "image/png")
    first = publish(store, PublishRequest(op_id="op_a", idempotency_key="k" * 8, artifacts={"image": art.id},
                                          preview_role=None, origin=Origin.generated, kind=Kind.concept_art,
                                          new_asset=NewAsset("tavern", "Tavern", Kind.concept_art,
                                                             Origin.generated, None)))
    glb = store.register_artifact(b"glTF-not-really", "model", "model/gltf-binary")
    with pytest.raises(KindMismatch):
        publish(store, PublishRequest(op_id="op_b", idempotency_key="k" * 8, artifacts={"model": glb.id},
                                      preview_role=None, origin=Origin.generated, kind=Kind.model3d,
                                      asset_id=first.asset_id, expected_current_version=first.version_id))
    with pytest.raises(NameTaken):
        publish(store, PublishRequest(op_id="op_c", idempotency_key="k" * 8, artifacts={"image": art.id},
                                      preview_role=None, origin=Origin.generated, kind=Kind.concept_art,
                                      new_asset=NewAsset("tavern", "Tavern 2", Kind.concept_art,
                                                         Origin.generated, None)))

