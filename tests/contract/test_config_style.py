"""Config effect report, non-fatal inert-field warnings and immutable style revision history."""
from __future__ import annotations

from typing import Any

from tests.conftest import Api, new_project

P = "/api/v1/projects"


def _patch(api: Api, pid: str, fn: Any) -> dict:
    view = api.get(f"{P}/{pid}/config")
    cfg = view["config"]
    fn(cfg)
    r = api.raw("PATCH", f"{P}/{pid}/config", json={"expected_revision": view["revision"], "config": cfg})
    assert r.status_code == 200, r.text
    return r.json()


def test_inert_fields_warn_but_save(api: Api) -> None:
    pid = new_project(api)
    out = _patch(api, pid, lambda c: c["defaults"].update(kind="model3d", build_profile="painted"))
    assert {"path": "defaults.build_profile", "message": out["warnings"][0]["message"]} in out["warnings"]
    fx = api.get(f"{P}/{pid}/config:effects")["effects"]
    assert next(e for e in fx if e["field"] == "build_profile")["classification"] == "unsupported"
    assert next(e for e in fx if e["field"] == "parameters.triangles")["classification"] == "applied"


def test_effects_scope_errors(api: Api) -> None:
    pid = new_project(api)
    assert api.raw("GET", f"{P}/{pid}/config:effects", params={"category_id": "nope"}).status_code == 404
    assert api.raw("GET", f"{P}/{pid}/config:effects").status_code == 422  # no kind anywhere
    fx = api.get(f"{P}/{pid}/config:effects", params={"kind": "icon", "mode": "edit"})
    assert fx["recipe"]["kind"] == "icon" and fx["mode"] == "edit"


def test_style_revisions_are_immutable_history(api: Api) -> None:
    pid = new_project(api)
    _patch(api, pid, lambda c: c["styles"].update(fantasy={"guide": "painterly v1"}))
    _patch(api, pid, lambda c: c["styles"]["fantasy"].update(guide="painterly v2"))
    revs = api.get(f"{P}/{pid}/styles/fantasy/revisions")["revisions"]
    assert [r["content"]["guide"] for r in revs] == ["painterly v2", "painterly v1"]
    assert [r["current"] for r in revs] == [True, False]
    assert revs[0]["actor"] == "operator" and revs[0]["config_revision"] > revs[1]["config_revision"]
    # restore = save the old content again: no duplicate record, the old one becomes current
    _patch(api, pid, lambda c: c["styles"]["fantasy"].update(guide="painterly v1"))
    revs = api.get(f"{P}/{pid}/styles/fantasy/revisions")["revisions"]
    assert len(revs) == 2 and next(r for r in revs if r["current"])["content"]["guide"] == "painterly v1"


def test_deleted_style_keeps_history_unknown_is_404(api: Api) -> None:
    pid = new_project(api)
    _patch(api, pid, lambda c: c["styles"].update(old={"guide": "g"}))
    _patch(api, pid, lambda c: c["styles"].pop("old"))
    revs = api.get(f"{P}/{pid}/styles/old/revisions")["revisions"]
    assert len(revs) == 1 and revs[0]["current"] is False
    assert api.raw("GET", f"{P}/{pid}/styles/never/revisions").status_code == 404
    assert api.raw("GET", f"{P}/{pid}/styles/..%2Fx/revisions").status_code == 404
