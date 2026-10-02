"""Asset archive / restore: hidden from active views, fully recoverable, never deleted. (#54)"""
from __future__ import annotations

from tests.conftest import Api, new_project, png_bytes


def _asset(api: Api, pid: str, name: str, color: tuple[int, int, int] = (200, 80, 40), **commit: object) -> dict:
    prev = api.post(f"/api/v1/projects/{pid}/imports:preview", files={"file": (f"{name}.png", png_bytes(color=color))})
    return api.post(f"/api/v1/projects/{pid}/imports:commit", {
        "import_id": prev["import_id"], "name": name, "kind": "concept_art",
        "idempotency_key": f"imp-{name}-0001", **commit})


def _rev(api: Api, pid: str, asset_id: str) -> int:
    return api.get(f"/api/v1/projects/{pid}/assets/{asset_id}")["manifest"]["revision"]


def _archive(api: Api, pid: str, asset_id: str, action: str = "archive", rev: int | None = None) -> dict:
    return api.post(f"/api/v1/projects/{pid}/assets/{asset_id}:{action}",
                    {"expected_revision": rev if rev is not None else _rev(api, pid, asset_id)})


def test_archive_hides_and_restore_returns_same_asset(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    keep, gone = _asset(api, pid, "Keep"), _asset(api, pid, "Gone", color=(1, 2, 3))
    base = api.get(f"/api/v1/projects/{pid}/assets/{gone['asset_id']}")

    out = _archive(api, pid, gone["asset_id"])
    assert out["archived_at"] and out["revision"] == base["manifest"]["revision"] + 1

    active = api.get(f"/api/v1/projects/{pid}/assets")
    assert [i["asset_id"] for i in active["items"]] == [keep["asset_id"]]
    assert active["total"] == 1 and active["all_assets_total"] == 1 and active["archived_total"] == 1
    assert api.get(f"/api/v1/projects/{pid}/assets", params={"q": "gone"})["total"] == 0
    assert api.get(f"/api/v1/projects/{pid}/assets", params={"group_by": "family"})["matching_asset_count"] == 1
    archived = api.get(f"/api/v1/projects/{pid}/assets", params={"archived": True})
    assert [i["asset_id"] for i in archived["items"]] == [gone["asset_id"]] and archived["planned_total"] == 0

    detail = api.get(f"/api/v1/projects/{pid}/assets/{gone['asset_id']}")  # still readable by id
    assert detail["manifest"]["archived_at"] and detail["files"] == base["files"]

    back = _archive(api, pid, gone["asset_id"], "restore")
    assert back["archived_at"] is None and back["asset_id"] == gone["asset_id"]
    restored = api.get(f"/api/v1/projects/{pid}/assets/{gone['asset_id']}")
    assert restored["files"] == base["files"] and restored["shown_version"] == base["shown_version"]
    assert api.get(f"/api/v1/projects/{pid}/assets")["total"] == 2
    assert api.get(f"/api/v1/projects/{pid}/assets", params={"archived": True})["total"] == 0


def test_archive_is_idempotent_and_revision_checked(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    a = _asset(api, pid, "A")
    first = _archive(api, pid, a["asset_id"])
    again = _archive(api, pid, a["asset_id"], rev=1)  # already archived: no-op even with a stale revision
    assert again["revision"] == first["revision"] and again["archived_at"] == first["archived_at"]
    r = api.raw("POST", f"/api/v1/projects/{pid}/assets/{a['asset_id']}:restore", json={"expected_revision": 1})
    assert r.status_code == 409  # restoring with a stale revision is refused
    assert api.raw("POST", f"/api/v1/projects/{pid}/assets/ast_{'0' * 8}:archive",
                   json={"expected_revision": 1}).status_code in (400, 404)


def test_archived_counts_and_new_versions(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    cfg = api.get(f"/api/v1/projects/{pid}/config")
    c = cfg["config"]
    c["categories"] = [{"id": "props", "slug": "props", "label": "Props"}]
    assert api.raw("PATCH", f"/api/v1/projects/{pid}/config",
                   json={"expected_revision": cfg["revision"], "config": c}).status_code == 200
    a = _asset(api, pid, "A", category_id="props")
    assert api.get(f"/api/v1/projects/{pid}/categories")["categories"][0]["count"] == 1
    _archive(api, pid, a["asset_id"])
    assert api.get(f"/api/v1/projects/{pid}/categories")["categories"][0]["count"] == 0

    prev = api.post(f"/api/v1/projects/{pid}/imports:preview", files={"file": ("b.png", png_bytes(color=(9, 9, 9)))})
    r = api.raw("POST", f"/api/v1/projects/{pid}/imports:commit", json={
        "import_id": prev["import_id"], "name": "A", "kind": "concept_art", "target_asset_id": a["asset_id"],
        "expected_current_version": a["version_id"], "idempotency_key": "key-arch-2"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "asset_archived"

    _archive(api, pid, a["asset_id"], "restore")
    assert api.get(f"/api/v1/projects/{pid}/categories")["categories"][0]["count"] == 1
    ok = api.raw("POST", f"/api/v1/projects/{pid}/imports:commit", json={
        "import_id": prev["import_id"], "name": "A", "kind": "concept_art", "target_asset_id": a["asset_id"],
        "expected_current_version": a["version_id"], "idempotency_key": "key-arch-2"})
    assert ok.status_code == 200 and ok.json()["display_version"] == 2


def test_archive_survives_index_rebuild(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    a, b = _asset(api, pid, "A"), _asset(api, pid, "B", color=(5, 5, 5))
    _archive(api, pid, b["asset_id"])
    assert api.post(f"/api/v1/projects/{pid}/storage:rebuild-index")["indexed"] == 2
    assert api.get(f"/api/v1/projects/{pid}/assets")["total"] == 1
    assert api.get(f"/api/v1/projects/{pid}/assets", params={"archived": True})["total"] == 1
    assert a["asset_id"] != b["asset_id"]
