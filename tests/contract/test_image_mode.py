"""Image-driven items (#52): a source image replaces prompt enhancement and preview generation.
SIMULATED engines only: contract evidence, never model-quality evidence."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.conftest import HEADERS, Api, png_bytes
from tests.contract.test_api_rounds_refs import V2, _approve, _item, _upload
from tests.contract.test_jobs_batches import _job, _setup, _studio


def _image_job(api: Api, pid: str, key: str, cat: str, source: dict[str, Any], **item: Any) -> dict:
    out = api.post(f"{V2}/{pid}/jobs", {
        "title": "from image", "category_id": cat, "idempotency_key": f"{key}-job", "run": True,
        "items": [{"name": "Crate", "brief": "", "generation_mode": "image", "source_image": source, **item}]})
    return out


def test_image_only_job_skips_enhancement_and_previews(make_api, tmp_path: Path) -> None:
    api, engine, aux, _ = _studio(make_api, tmp_path)
    pid = _setup(api)
    art = _upload(api, pid)
    out = _image_job(api, pid, "img-one", "props", {"artifact_id": art})
    jid = out["job"]["id"]
    api.wait_ops()
    item = _item(api, pid, jid)
    assert item["generation_mode"] == "image" and item["source_image"]["artifact_id"] == art
    assert "enhance" not in aux.calls and engine.calls == []  # no enhancer call, no image-engine submission
    assert item["prompt"]["origin"] == "brief" and item["prompt_variants"] == []
    rnd = item["rounds"][0]
    assert len(rnd["candidates"]) == 1 and rnd["requested"] == 1 and not rnd["generating"]
    cand = rnd["candidates"][0]
    assert cand["artifact_id"] == art and cand["qa"] is not None  # the source itself, QA'd like any candidate
    assert cand["engine"]["source_image"]["artifact_id"] == art
    assert item["candidate_set"]["generation"]["mode"] == "image" and item["candidate_set"]["generation"]["engine"] is None
    assert not any(item["legal"][k] for k in ("edit_prompt", "enhance", "confirm", "regenerate"))
    assert item["legal"]["approve"]

    item = _approve(api, pid, jid, rnd, 0, "img-one-approve")  # approval and build work as for generated candidates
    assert item["approved"]["candidate_id"] == cand["id"]
    api.post(f"{V2}/{pid}/jobs/{jid}:build-approved", {"idempotency_key": "img-one-build", "items": [{
        "item_id": item["id"], "approval_id": item["approval"], "expected_item_revision": item["revision"]}]})
    api.wait_ops()
    built = _item(api, pid, jid)["build"]
    assert built is not None and built["result"] == "valid", built
    assert engine.calls == []  # still no generation anywhere


def test_media_library_source_keeps_its_provenance(make_api, tmp_path: Path) -> None:
    api, _, _, _ = _studio(make_api, tmp_path)
    pid = _setup(api)
    up = api.c.post(f"/api/v1/projects/{pid}/media:upload", files=[("files", ("moodboard.png", png_bytes()))],
                    headers=HEADERS).json()["results"][0]["item"]
    jid = _image_job(api, pid, "img-media", "props", {"media_id": up["id"]})["job"]["id"]
    api.wait_ops()
    item = _item(api, pid, jid)
    assert item["source_image"]["origin"] == "media" and item["source_image"]["media_id"] == up["id"]
    prov = item["rounds"][0]["candidates"][0]["engine"]["source_image"]
    assert prov["media_id"] == up["id"] and prov["origin"] == "media"


def test_invalid_combinations_are_refused_with_a_reason(make_api, tmp_path: Path) -> None:
    api, _, _, _ = _studio(make_api, tmp_path)
    pid = _setup(api)
    art = _upload(api, pid)

    def create(**item: Any):  # noqa: ANN202
        return api.raw("POST", f"{V2}/{pid}/jobs", json={"title": "t", "category_id": "props",
                                                         "idempotency_key": f"bad-{len(str(item))}-{id(item)}",
                                                         "items": [{"name": "A", "brief": "x", **item}]})
    r = create(generation_mode="prompt_image", source_image={"artifact_id": art})
    assert r.status_code == 422 and r.json()["error"]["code"] == "unsupported_generation_mode"
    r = create(generation_mode="image")
    assert r.status_code == 422 and r.json()["error"]["code"] == "source_image_required"
    r = create(source_image={"artifact_id": art})
    assert r.status_code == 422 and r.json()["error"]["code"] == "source_image_unexpected"
    r = create(generation_mode="image", source_image={"artifact_id": art, "media_id": "med_x"})
    assert r.status_code in (400, 422)  # exactly one source
    r = create(generation_mode="image", source_image={"artifact_id": "art_" + "0" * 16})
    assert r.status_code == 422  # not an existing image artifact


def test_prompt_stage_commands_are_refused_for_image_items(make_api, tmp_path: Path) -> None:
    api, _, aux, _ = _studio(make_api, tmp_path)
    pid = _setup(api)
    jid = _image_job(api, pid, "img-cmd", "props", {"artifact_id": _upload(api, pid)})["job"]["id"]
    api.wait_ops()
    item = _item(api, pid, jid)
    ref = {"item_id": item["id"], "expected_item_revision": item["revision"]}
    out = api.post(f"{V2}/{pid}/jobs/{jid}:edit-prompts", {"items": [{**ref, "description": "x"}]})
    assert out["results"][0]["code"] == "image_mode"
    out = api.post(f"{V2}/{pid}/jobs/{jid}:regenerate", {"idempotency_key": "img-cmd-regen",
                                                          "items": [ref]})
    assert out["results"][0]["code"] == "image_mode"
    out = api.post(f"{V2}/{pid}/jobs/{jid}:confirm-and-generate", {"idempotency_key": "img-cmd-conf", "items": [
        {**ref, "prompt_revision_id": item["current_prompt"]}]})
    assert out["results"][0]["code"] == "image_mode"
    r = api.raw("POST", f"{V2}/{pid}/jobs/{jid}:enhance", json={"item_ids": [item["id"]],
                                                                 "idempotency_key": "img-cmd-enh1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "nothing_eligible"
    assert "enhance" not in aux.calls


def test_prompt_mode_is_unchanged(make_api, tmp_path: Path) -> None:
    api, _, aux, _ = _studio(make_api, tmp_path)
    pid = _setup(api)
    jid = _job(api, pid, "concept", ["Tavern"], "img-prompt-job", candidate_count=2)
    item = _item(api, pid, jid)
    assert item["generation_mode"] == "prompt" and item["source_image"] is None
    api.post(f"{V2}/{pid}/jobs/{jid}:run", {"idempotency_key": "img-prompt-run"})
    api.wait_ops()
    assert aux.calls.count("enhance") == 2  # prompt flow: enhancer runs (once per preview slot)
