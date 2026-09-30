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
    out = _patch(api, pid, lambda c: c["defaults"].update(
        kind="model3d", budget={"mode": "value", "value": {"size_px": {"max": 64}}}))
    assert "defaults.budget.size_px" in {w["path"] for w in out["warnings"]}
    fx = api.get(f"{P}/{pid}/config:effects")["effects"]
    assert next(e for e in fx if e["field"] == "budget.size_px")["classification"] == "unsupported"
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


def test_undefined_build_profile_is_rejected(api: Api) -> None:
    pid = new_project(api)
    view = api.get(f"{P}/{pid}/config")
    cfg = view["config"]
    cfg["defaults"]["build_profile"] = {"mode": "value", "value": "painted"}
    r = api.raw("PATCH", f"{P}/{pid}/config", json={"expected_revision": view["revision"], "config": cfg})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_config"


def test_build_profile_saves_and_reports_effects(api: Api) -> None:
    pid = new_project(api)

    def edit(c: dict) -> None:
        c["build_profiles"]["foliage"] = {"material": {"alpha_mode": "mask", "alpha_cutoff": 0.5}}
        c["defaults"].update(kind="model3d", build_profile={"mode": "value", "value": "foliage"})

    _patch(api, pid, edit)
    fx = api.get(f"{P}/{pid}/config:effects")["effects"]
    e = next(e for e in fx if e["field"] == "build_profile.material.alpha_mode")
    assert e["classification"] == "applied" and e["value"] == "mask"


def test_effects_style_preview(api: Api) -> None:
    pid = new_project(api)
    _patch(api, pid, lambda c: (c["styles"].update(ink={"guide": "bold ink"}), c["defaults"].update(kind="model3d")))
    out = api.get(f"{P}/{pid}/config:effects", params={"style": "ink"})
    assert out["style_id"] == "ink"
    g = next(e for e in out["effects"] if e["field"] == "style.guide")
    assert g["value"] == "bold ink" and g["source"] == "preview"
    assert api.get(f"{P}/{pid}/config:effects")["style_id"] is None
    r = api.raw("GET", f"{P}/{pid}/config:effects", params={"style": "nope"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_style"
