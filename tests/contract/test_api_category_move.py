"""Assign, move, clear and bulk-move asset categories without touching identity, versions or files. (#56)"""
from __future__ import annotations

from tests.conftest import Api, new_project, png_bytes


def _project(api: Api) -> str:
    pid = new_project(api)
    cfg = api.get(f"/api/v1/projects/{pid}/config")
    c = cfg["config"]
    c["categories"] = [{"id": "props", "slug": "props", "label": "Props"},
                       {"id": "terrain", "slug": "terrain", "label": "Terrain"}]
    r = api.raw("PATCH", f"/api/v1/projects/{pid}/config", json={"expected_revision": cfg["revision"], "config": c})
    assert r.status_code == 200, r.text
    return pid


def _asset(api: Api, pid: str, name: str, color: tuple[int, int, int], **commit: object) -> str:
    prev = api.post(f"/api/v1/projects/{pid}/imports:preview", files={"file": (f"{name}.png", png_bytes(color=color))})
    return api.post(f"/api/v1/projects/{pid}/imports:commit", {
        "import_id": prev["import_id"], "name": name, "kind": "concept_art",
        "idempotency_key": f"imp-{name}-0001", **commit})["asset_id"]


def _move(api: Api, pid: str, ids: list[str], category: str | None):  # noqa: ANN202
    return api.raw("POST", f"/api/v1/projects/{pid}/assets:set-category", json={"asset_ids": ids, "category_id": category})


def _counts(api: Api, pid: str) -> dict[str, int]:
    return {c["id"]: c["count"] for c in api.get(f"/api/v1/projects/{pid}/categories")["categories"]}


def test_assign_move_and_clear_keep_everything_else(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = _project(api)
    a = _asset(api, pid, "A", (1, 1, 1))
    before = api.get(f"/api/v1/projects/{pid}/assets/{a}")
    assert before["manifest"]["category_id"] is None and _counts(api, pid) == {"props": 0, "terrain": 0}

    out = _move(api, pid, [a], "props").json()
    assert out["changed"] == 1 and out["results"][0]["ok"] and _counts(api, pid)["props"] == 1
    out = _move(api, pid, [a], "terrain").json()
    assert out["changed"] == 1 and _counts(api, pid) == {"props": 0, "terrain": 1}
    assert api.get(f"/api/v1/projects/{pid}/assets", params={"category_id": "terrain"})["total"] == 1
    assert api.get(f"/api/v1/projects/{pid}/assets", params={"category_id": "props"})["total"] == 0
    assert _move(api, pid, [a], None).json()["changed"] == 1  # explicit return to Uncategorized
    after = api.get(f"/api/v1/projects/{pid}/assets/{a}")
    assert after["manifest"]["category_id"] is None and after["category_label"] is None
    assert after["files"] == before["files"] and after["shown_version"] == before["shown_version"]
    assert after["manifest"]["versions"] == before["manifest"]["versions"] and after["manifest"]["asset_id"] == a


def test_bulk_move_reports_each_asset_and_is_idempotent(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = _project(api)
    ids = [_asset(api, pid, n, c) for n, c in (("A", (1, 1, 1)), ("B", (2, 2, 2)), ("C", (3, 3, 3)))]
    out = _move(api, pid, [*ids, "ast_" + "0" * 16, ids[0]], "props").json()  # unknown id + a duplicate
    assert out["changed"] == 3 and [r["ok"] for r in out["results"]] == [True, True, True, False]
    assert out["results"][3]["code"] == "not_found"
    assert _counts(api, pid)["props"] == 3
    again = _move(api, pid, ids, "props").json()
    assert again["changed"] == 0 and all(r["ok"] and not r["changed"] for r in again["results"])
    assert api.get(f"/api/v1/projects/{pid}/assets", params={"category_id": "props"})["total"] == 3


def test_category_must_exist_and_be_active_and_ids_valid(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = _project(api)
    a = _asset(api, pid, "A", (1, 1, 1))
    assert _move(api, pid, [a], "nope").json()["error"]["code"] == "unknown_category"
    cfg = api.get(f"/api/v1/projects/{pid}/config")
    c = cfg["config"]
    c["categories"][1]["archived"] = True
    api.raw("PATCH", f"/api/v1/projects/{pid}/config", json={"expected_revision": cfg["revision"], "config": c})
    assert _move(api, pid, [a], "terrain").json()["error"]["code"] == "category_archived"
    assert _move(api, pid, ["not-an-id"], "props").status_code == 400
    assert _move(api, pid, [], "props").status_code == 400


def test_new_category_is_assignable_immediately_and_survives_rebuild(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = _project(api)
    a = _asset(api, pid, "A", (1, 1, 1), category_id="props")
    cfg = api.get(f"/api/v1/projects/{pid}/config")
    c = cfg["config"]
    c["categories"].append({"id": "fresh", "slug": "fresh", "label": "Fresh"})
    api.raw("PATCH", f"/api/v1/projects/{pid}/config", json={"expected_revision": cfg["revision"], "config": c})
    assert "fresh" in _counts(api, pid)
    assert _move(api, pid, [a], "fresh").json()["changed"] == 1
    api.post(f"/api/v1/projects/{pid}/storage:rebuild-index")
    assert _counts(api, pid)["fresh"] == 1 and _counts(api, pid)["props"] == 0
