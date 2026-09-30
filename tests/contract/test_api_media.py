"""Project media library: upload/dedupe/list/patch/archive and guidance-only reference integration."""
from __future__ import annotations

import io
from typing import Any

from PIL import Image

from tests.conftest import HEADERS, Api, new_project, png_bytes
from tests.contract.test_api_rounds_refs import _detail, _one_job
from tests.contract.test_jobs_batches import _setup

P = "/api/v1/projects"
V2 = "/api/v2/projects"


def _enc(fmt: str, mode: str = "RGB", color: Any = (10, 120, 200)) -> bytes:
    out = io.BytesIO()
    Image.new(mode, (32, 24), color).save(out, fmt)
    return out.getvalue()


def _upload(api: Api, pid: str, files: list[tuple[str, bytes]]) -> Any:
    return api.c.post(f"{P}/{pid}/media:upload", files=[("files", f) for f in files], headers=HEADERS)


def _one(api: Api, pid: str, name: str = "a.png", data: bytes | None = None) -> dict:
    r = _upload(api, pid, [(name, data or png_bytes())])
    assert r.status_code == 200, r.text
    (res,) = r.json()["results"]
    assert res["ok"], res
    return res["item"]


def _patch(api: Api, pid: str, mid: str, **body: Any) -> Any:
    return api.raw("PATCH", f"{P}/{pid}/media/{mid}", json=body)


def test_formats_and_per_file_rejection(make_api) -> None:
    api = make_api(coordinator=False)
    pid = new_project(api)
    r = _upload(api, pid, [("a.png", png_bytes()), ("b.jpg", _enc("JPEG")),
                           ("c.webp", _enc("WEBP", "RGBA", (1, 2, 3, 100))), ("d.gif", _enc("GIF", "P", 1)),
                           ("e.png", b"not an image")])
    assert r.status_code == 200, r.text
    res = r.json()["results"]
    assert [x["ok"] for x in res] == [True, True, True, False, False]
    assert res[0]["item"]["format"] == "PNG" and res[1]["item"]["format"] == "JPEG"
    web = res[2]["item"]
    assert web["format"] == "PNG" and web["has_alpha"] and not res[2]["duplicate"]
    art = api.get(f"{P}/{pid}/artifacts/{web['artifact_id']}")
    assert art["mime"] == "image/png" and art["meta"]["source_format"] == "WEBP" and art["role"] == "reference"
    assert api.get(f"{P}/{pid}/artifacts/{web['thumb_artifact_id']}")["role"] == "preview"
    assert res[3]["error"]["code"] == res[4]["error"]["code"] == "invalid_image"
    assert len(api.get(f"{P}/{pid}/media")["items"]) == 3


def test_cmyk_jpeg_gets_thumbnail(make_api) -> None:
    api = make_api(coordinator=False)
    pid = new_project(api)
    item = _one(api, pid, "print.jpg", _enc("JPEG", "CMYK", (0, 80, 160, 10)))
    assert item["format"] == "JPEG"
    assert api.get(f"{P}/{pid}/artifacts/{item['thumb_artifact_id']}")["mime"] == "image/png"


def test_too_many_files(make_api) -> None:
    api = make_api(coordinator=False)
    pid = new_project(api)
    r = _upload(api, pid, [(f"{n}.png", png_bytes(color=(n, 0, 0))) for n in range(21)])
    assert r.status_code == 422 and r.json()["error"]["code"] == "too_many_files"


def test_duplicate_upload_same_id(make_api) -> None:
    api = make_api(coordinator=False)
    pid = new_project(api)
    first = _one(api, pid, "Castle Wall.png")
    assert first["name"] == "Castle Wall"
    (res,) = _upload(api, pid, [("other.png", png_bytes())]).json()["results"]
    assert res["ok"] and res["duplicate"] is True and res["item"]["id"] == first["id"]
    assert res["item"]["name"] == "Castle Wall" and len(api.get(f"{P}/{pid}/media")["items"]) == 1


def test_list_filters_and_tag_counts(make_api) -> None:
    api = make_api(coordinator=False)
    pid = new_project(api)
    a = _one(api, pid, "a.png", png_bytes(color=(1, 1, 1)))
    b = _one(api, pid, "b.png", png_bytes(color=(2, 2, 2)))
    c = _one(api, pid, "c.png", png_bytes(color=(3, 3, 3)))
    assert _patch(api, pid, a["id"], expected_revision=1, name="Stone Tower", tags=["Wall", "stone"]).status_code == 200
    assert _patch(api, pid, b["id"], expected_revision=1, note="mossy STONE path", tags=["stone"]).status_code == 200
    assert _patch(api, pid, c["id"], expected_revision=1, tags=["wall"]).status_code == 200
    out = api.get(f"{P}/{pid}/media")
    assert out["tags"] == [{"tag": "stone", "count": 2}, {"tag": "wall", "count": 2}]
    assert {i["id"] for i in api.get(f"{P}/{pid}/media", params={"q": "TOWER"})["items"]} == {a["id"]}
    assert {i["id"] for i in api.get(f"{P}/{pid}/media", params={"q": "mossy"})["items"]} == {b["id"]}
    assert {i["id"] for i in api.get(f"{P}/{pid}/media", params={"q": "stone"})["items"]} == {a["id"], b["id"]}
    assert {i["id"] for i in api.get(f"{P}/{pid}/media", params={"tag": "Wall"})["items"]} == {a["id"], c["id"]}
    filtered = api.get(f"{P}/{pid}/media", params={"tag": "wall"})
    assert filtered["tags"] == out["tags"]  # counts ignore q/tag filters


def test_patch_normalizes_and_validates(make_api) -> None:
    api = make_api(coordinator=False)
    pid = new_project(api)
    m = _one(api, pid)
    r = _patch(api, pid, m["id"], expected_revision=1, name="  New  ", tags=[" Foo ", "foo", "", "BAR"],
               source_url="https://x.example/a", source_rights="CC0")
    body = r.json()
    assert r.status_code == 200 and body["name"] == "New" and body["tags"] == ["foo", "bar"]
    assert body["revision"] == 2 and body["source_rights"] == "CC0" and body["note"] == ""
    assert _patch(api, pid, m["id"], expected_revision=2, source_url="ftp://x").status_code == 422
    assert _patch(api, pid, m["id"], expected_revision=2, tags=["x" * 41]).status_code == 422
    assert _patch(api, pid, m["id"], expected_revision=2, tags=[str(n) for n in range(21)]).status_code == 422
    assert _patch(api, pid, m["id"], expected_revision=2, name="   ").status_code == 422
    stale = _patch(api, pid, m["id"], expected_revision=1, name="x")
    assert stale.status_code == 409
    assert api.get(f"{P}/{pid}/media/{m['id']}")["name"] == "New"


def test_archive_restore(make_api) -> None:
    api = make_api(coordinator=False)
    pid = new_project(api)
    m = _one(api, pid)
    arch = api.post(f"{P}/{pid}/media/{m['id']}:archive", {"expected_revision": 1})
    assert arch["archived_at"] and arch["revision"] == 2
    assert api.get(f"{P}/{pid}/media")["items"] == []
    assert [i["id"] for i in api.get(f"{P}/{pid}/media", params={"archived": 1})["items"]] == [m["id"]]
    again = api.post(f"{P}/{pid}/media/{m['id']}:archive", {"expected_revision": 2})
    assert again["revision"] == 2
    rest = api.post(f"{P}/{pid}/media/{m['id']}:restore", {"expected_revision": 2})
    assert rest["archived_at"] is None and rest["revision"] == 3
    assert len(api.get(f"{P}/{pid}/media")["items"]) == 1
    stale = api.raw("POST", f"{P}/{pid}/media/{m['id']}:archive", json={"expected_revision": 1})
    assert stale.status_code == 409


def test_references_upload_registers_media(make_api) -> None:
    api = make_api(coordinator=False)
    pid = new_project(api)
    r = api.c.post(f"{P}/{pid}/references:upload", files={"file": ("ref.png", png_bytes())}, headers=HEADERS)
    body = r.json()
    assert r.status_code == 200 and body["duplicate"] is False and body["format"] == "PNG" and body["width"] == 64
    (item,) = api.get(f"{P}/{pid}/media")["items"]
    assert item["id"] == body["media_id"] and item["artifact_id"] == body["artifact_id"]
    again = api.c.post(f"{P}/{pid}/references:upload", files={"file": ("ref.png", png_bytes())}, headers=HEADERS)
    assert again.json()["duplicate"] is True and again.json()["artifact_id"] == body["artifact_id"]


def test_media_as_job_and_item_reference(make_api) -> None:
    api = make_api(coordinator=False)
    pid = _setup(api)
    m = _one(api, pid)
    out = api.post(f"{V2}/{pid}/jobs", {"title": "t", "category_id": "concept", "idempotency_key": "media-job-1",
                                        "items": [{"name": "A", "brief": "a mill",
                                                   "references": [{"media_id": m["id"], "note": "n"}]}]})
    (ref,) = _detail(api, pid, out["job"]["id"])["items"][0]["references"]
    assert ref["origin"] == "media" and ref["media_id"] == m["id"] and ref["artifact_id"] == m["artifact_id"]
    both = api.raw("POST", f"{V2}/{pid}/jobs", json={
        "title": "t", "category_id": "concept", "idempotency_key": "media-job-2",
        "items": [{"name": "A", "references": [{"media_id": m["id"], "artifact_id": m["artifact_id"]}]}]})
    assert both.status_code in (400, 422)

    jid2, item = _one_job(api, pid, "media-job-3")
    base = f"{V2}/{pid}/jobs/{jid2}/items/{item['id']}"
    added = api.post(f"{base}:add-reference", {"media_id": m["id"], "expected_item_revision": item["revision"]})
    assert added["references"][0]["origin"] == "media" and added["references"][0]["media_id"] == m["id"]
    bad = api.raw("POST", f"{base}:add-reference", json={"media_id": m["id"], "artifact_id": m["artifact_id"],
                                                        "expected_item_revision": added["revision"]})
    assert bad.status_code in (400, 422)


def test_archived_and_unknown_media_refused(make_api) -> None:
    api = make_api(coordinator=False)
    pid = _setup(api)
    m = _one(api, pid)
    jid, item = _one_job(api, pid, "media-arch-1")
    base = f"{V2}/{pid}/jobs/{jid}/items/{item['id']}"
    api.post(f"{P}/{pid}/media/{m['id']}:archive", {"expected_revision": 1})
    r = api.raw("POST", f"{base}:add-reference", json={"media_id": m["id"], "expected_item_revision": item["revision"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "media_archived"
    unknown = "med_" + "0" * 16
    r = api.raw("POST", f"{base}:add-reference", json={"media_id": unknown,
                                                       "expected_item_revision": item["revision"]})
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_media"
    r = api.raw("POST", f"{base}:add-reference", json={"media_id": "nope", "expected_item_revision": item["revision"]})
    assert r.status_code == 422
    assert api.raw("GET", f"{P}/{pid}/media/{unknown}").status_code == 404


def test_summary_counts_media(make_api) -> None:
    api = make_api(coordinator=False)
    pid = new_project(api)
    assert api.get(f"{P}/{pid}/summary")["counts"]["media"] == 0
    a = _one(api, pid, "a.png", png_bytes(color=(1, 1, 1)))
    _one(api, pid, "b.png", png_bytes(color=(2, 2, 2)))
    assert api.get(f"{P}/{pid}/summary")["counts"]["media"] == 2
    api.post(f"{P}/{pid}/media/{a['id']}:archive", {"expected_revision": 1})
    assert api.get(f"{P}/{pid}/summary")["counts"]["media"] == 1
