"""Project reference sets reach their consumers by mode; image_conditioning is blocked; caps and exclusions are
recorded. SIMULATED engines only: contract evidence, not model quality."""
from __future__ import annotations

import hashlib
from typing import Any

import pytest
from assetstudio_core import inheritance

from tests.conftest import Api, new_project, png_bytes
from tests.contract.test_api_rounds_refs import V2, _confirm, _enhance, _item, _upload
from tests.contract.test_api_variants_generate import _variant_jobs
from tests.contract.test_jobs_batches import CATEGORIES, _job


def _sha(color: tuple[int, int, int]) -> str:
    return hashlib.sha256(png_bytes(color=color)).hexdigest()


def _project(api: Api, mode: str, color: tuple[int, int, int] = (10, 200, 30)) -> str:
    pid = new_project(api)
    art = _upload(api, pid, color)
    cfg = api.get(f"/api/v1/projects/{pid}/config")["config"]
    cfg["categories"] = CATEGORIES
    cfg["reference_sets"] = {"moodboard": {"label": "Mood", "mode": mode, "images": [
        {"artifact_id": art, "label": "moody", "role": "style"}]}}
    cfg["defaults"]["reference_set"] = "moodboard"
    r = api.raw("PATCH", f"/api/v1/projects/{pid}/config", json={"expected_revision": 1, "config": cfg})
    assert r.status_code == 200, r.text
    return pid


def _concept(api: Api, pid: str, key: str) -> str:
    return _job(api, pid, "concept", ["Tavern"], key, candidate_count=2)


def test_t01_prompt_guidance_set_reaches_enhancer(make_api) -> None:
    api = make_api()
    pid = _project(api, "prompt_guidance")
    jid = _concept(api, pid, "style-routing-1")
    item = _enhance(api, pid, jid, "style-routing-1-enh")
    assert _sha((10, 200, 30)) in [s for _, s in api.studio.aux.enhance_images[-1]]
    assert "set:moodboard:0" in item["prompt"]["bindings"]["reference_ids"]


def test_t02_item_ref_first_then_set(make_api) -> None:
    api = make_api()
    pid = _project(api, "prompt_guidance")
    jid = _concept(api, pid, "style-routing-2")
    item = _item(api, pid, jid)
    api.post(f"{V2}/{pid}/jobs/{jid}/items/{item['id']}:add-reference", {
        "artifact_id": _upload(api, pid, (1, 2, 3)), "expected_item_revision": item["revision"]})
    item = _enhance(api, pid, jid, "style-routing-2-enh")
    assert [s for _, s in api.studio.aux.enhance_images[-1]] == [_sha((1, 2, 3)), _sha((10, 200, 30))]
    assert item["prompt"]["bindings"]["reference_ids"][1] == "set:moodboard:0"


def test_t03_qa_reference_set_goes_to_compare_only(make_api) -> None:
    api = make_api()
    pid = _project(api, "qa_reference")
    jid = _concept(api, pid, "style-routing-3")
    item = _enhance(api, pid, jid, "style-routing-3-enh")
    assert api.studio.aux.enhance_images[-1] == []
    assert item["prompt"]["bindings"]["reference_ids"] == []
    assert _confirm(api, pid, jid, "style-routing-3-confirm")["results"][0]["ok"]
    assert _sha((10, 200, 30)) in [s for call in api.studio.aux.compare_images for _, s in call]


def test_t04_image_conditioning_blocked_at_admission(make_api) -> None:
    api = make_api()
    pid = _project(api, "image_conditioning")
    jid = _concept(api, pid, "style-routing-4")
    _enhance(api, pid, jid, "style-routing-4-enh")
    res = _confirm(api, pid, jid, "style-routing-4-confirm")["results"][0]
    assert not res["ok"] and res["code"] == "reference_conditioning_unavailable", res


def test_t05_legacy_snapshot_excludes_set_images(make_api, monkeypatch: pytest.MonkeyPatch) -> None:
    real = inheritance.build_snapshot

    def legacy(*a: Any, **k: Any) -> dict[str, Any]:
        snap = real(*a, **k)
        snap.pop("reference_routing", None)
        return snap

    api = make_api()
    pid = _project(api, "prompt_guidance")
    monkeypatch.setattr(inheritance, "build_snapshot", legacy)
    monkeypatch.setattr("assetstudio_server.services.jobs.build_snapshot", legacy, raising=False)
    jid = _concept(api, pid, "style-routing-5")
    item = _enhance(api, pid, jid, "style-routing-5-enh")
    assert api.studio.aux.enhance_images[-1] == []
    b = item["prompt"]["bindings"]
    assert [e["id"] for e in b["references_excluded"]] == ["set:moodboard:0"]
    assert "not routed" in b["references_excluded"][0]["reason"]


def test_t06_variant_enhancer_never_exceeds_four_images(make_api) -> None:
    api = make_api()
    pid, _, created = _variant_jobs(api)
    jid = created["job_ids"][0]
    item = _item(api, pid, jid)
    rev = item["revision"]
    for n in range(4):
        rev = api.post(f"{V2}/{pid}/jobs/{jid}/items/{item['id']}:add-reference", {
            "artifact_id": _upload(api, pid, (n, 5, 5)), "expected_item_revision": rev})["revision"]
    item = _enhance(api, pid, jid, "style-routing-6-enh")
    assert len(api.studio.aux.enhance_images[-1]) == 4
    b = item["prompt"]["bindings"]
    assert len(b["reference_ids"]) == 3 and len(b["references_excluded"]) == 1
    assert "3-image limit" in b["references_excluded"][0]["reason"]


def test_t07_item_effects_report_routing(make_api) -> None:
    api = make_api()
    pid = _project(api, "prompt_guidance")
    jid = _concept(api, pid, "style-routing-7")
    item = _item(api, pid, jid)
    out = api.get(f"{V2}/{pid}/jobs/{jid}/items/{item['id']}/effects")
    assert out["planned"] is True and out["mode"] == "t2i"
    pg = out["references"]["prompt_guidance"]
    assert pg["limit"] == 4 and [b["id"] for b in pg["selected"]] == ["set:moodboard:0"]
    assert pg["selected"][0]["origin"] == "project_set" and out["references"]["qa_reference"]["selected"] == []
    fx = next(e for e in out["effects"] if e["field"] == "reference_set.moodboard")
    assert fx["classification"] == "conditioning_only"
