"""Permanent asset deletion: archived-only, deliberate, reference-aware, never leaves dangling data. (#55)"""
from __future__ import annotations

from pathlib import Path

from tests.conftest import Api, new_project, png_bytes


def _asset(api: Api, pid: str, name: str, color: tuple[int, int, int] = (200, 80, 40), run: int = 1) -> dict:
    prev = api.post(f"/api/v1/projects/{pid}/imports:preview", files={"file": (f"{name}.png", png_bytes(color=color))})
    return api.post(f"/api/v1/projects/{pid}/imports:commit", {
        "import_id": prev["import_id"], "name": name, "kind": "concept_art", "idempotency_key": f"imp-{name}-000{run}"})


def _detail(api: Api, pid: str, asset_id: str) -> dict:
    return api.get(f"/api/v1/projects/{pid}/assets/{asset_id}")


def _archive(api: Api, pid: str, asset_id: str) -> dict:
    rev = _detail(api, pid, asset_id)["manifest"]["revision"]
    return api.post(f"/api/v1/projects/{pid}/assets/{asset_id}:archive", {"expected_revision": rev})


def _delete(api: Api, pid: str, asset_id: str, **over: object):  # noqa: ANN202
    m = _detail(api, pid, asset_id)["manifest"]
    body = {"expected_revision": m["revision"], "confirm_name": m["name_id"], **over}
    return api.raw("POST", f"/api/v1/projects/{pid}/assets/{asset_id}:delete", json=body)


def _blob_files(api: Api, pid: str) -> set[Path]:
    root = Path(api.studio.registry.get(pid).root)
    return {p for p in (root / "blobs" / "sha256").rglob("*") if p.is_file()}


def test_only_archived_assets_with_the_exact_name_and_revision(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    a = _asset(api, pid, "Doomed")
    r = _delete(api, pid, a["asset_id"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "asset_not_archived"
    _archive(api, pid, a["asset_id"])
    assert _delete(api, pid, a["asset_id"], confirm_name="nope").json()["error"]["code"] == "confirmation_mismatch"
    r = _delete(api, pid, a["asset_id"], expected_revision=1)
    assert r.status_code == 409
    assert _detail(api, pid, a["asset_id"])["manifest"]["archived_at"]  # nothing was removed by the refusals


def test_delete_removes_everything_the_asset_owned(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    keep = _asset(api, pid, "Keep", color=(1, 2, 3))
    gone = _asset(api, pid, "Gone")
    files = _detail(api, pid, gone["asset_id"])["files"]
    before = _blob_files(api, pid)
    _archive(api, pid, gone["asset_id"])

    r = _delete(api, pid, gone["asset_id"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["asset_id"] == gone["asset_id"] and out["versions"] == 1 and out["blobs_deleted"] >= 1
    assert api.raw("GET", f"/api/v1/projects/{pid}/assets/{gone['asset_id']}").status_code == 404
    assert api.raw("GET", f"/api/v1/projects/{pid}/artifacts/{files[0]['artifact_id']}").status_code == 404
    assert len(_blob_files(api, pid)) < len(before)
    assert api.get(f"/api/v1/projects/{pid}/assets")["total"] == 1
    assert api.get(f"/api/v1/projects/{pid}/assets", params={"archived": True})["total"] == 0
    assert _detail(api, pid, keep["asset_id"])["files"]  # the other asset is untouched

    root = Path(api.studio.registry.get(pid).root)  # no orphaned records for the deleted id
    leftovers = [p for p in root.rglob("*.json") if gone["asset_id"] in p.read_text(errors="ignore")]
    assert leftovers == [], leftovers
    assert api.post(f"/api/v1/projects/{pid}/storage:rebuild-index")["indexed"] == 1
    again = _asset(api, pid, "Gone", run=2)  # the name is free again and the content can be re-imported
    assert again["asset_id"] and api.get(f"/api/v1/projects/{pid}/assets")["total"] == 2
    assert _detail(api, pid, again["asset_id"])["manifest"]["name_id"] == "gone"


def test_shared_artifacts_survive(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    a = _asset(api, pid, "Twin A")
    b = _asset(api, pid, "Twin B")  # same bytes, separate artifact records: the blob is shared
    sha = lambda x: {f["sha256"] for f in _detail(api, pid, x["asset_id"])["files"]}  # noqa: E731
    assert sha(a) == sha(b)
    _archive(api, pid, a["asset_id"])
    out = _delete(api, pid, a["asset_id"]).json()
    assert out["artifacts_deleted"] >= 1 and out["blobs_kept"] >= 1 and out["blobs_deleted"] == 0
    still = _detail(api, pid, b["asset_id"])["files"]
    art = still[0]["artifact_id"]
    r = api.raw("GET", f"/api/v1/projects/{pid}/artifacts/{art}/content")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"


def test_family_anchor_blocks_deletion_with_a_reason(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    a, b = _asset(api, pid, "Anchor"), _asset(api, pid, "Member", color=(9, 9, 9))
    api.post(f"/api/v1/projects/{pid}/families", {"name": "Set", "asset_ids": [a["asset_id"], b["asset_id"]],
                                                  "anchor_asset_id": a["asset_id"]})
    _archive(api, pid, a["asset_id"])
    r = _delete(api, pid, a["asset_id"])
    err = r.json()["error"]
    assert r.status_code == 409 and err["code"] == "asset_in_use"
    assert err["detail"][0]["type"] == "families" and err["detail"][0]["reason"] == "family anchor"
    assert _detail(api, pid, a["asset_id"])["files"]  # intact


def test_repeating_after_an_interrupted_delete_finishes_it(make_api) -> None:
    api = make_api(engine="none", coordinator=False)
    pid = new_project(api)
    a = _asset(api, pid, "Half")
    _archive(api, pid, a["asset_id"])
    ctx = api.studio.registry.get(pid)
    art = _detail(api, pid, a["asset_id"])["files"][0]
    ctx.store.repo.delete_blob(art["sha256"])  # a crash after the blob went but before the manifest did
    r = _delete(api, pid, a["asset_id"])
    assert r.status_code == 200, r.text
    assert api.raw("GET", f"/api/v1/projects/{pid}/assets/{a['asset_id']}").status_code == 404
