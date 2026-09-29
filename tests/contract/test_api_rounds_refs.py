"""Rounds (every candidate set kept, approve from any round) and per-item reference images / enhancement presets.
SIMULATED engines only: contract evidence, not model quality."""
from __future__ import annotations

from typing import Any

from tests.conftest import HEADERS, Api, png_bytes
from tests.contract.test_jobs_batches import _job, _setup

V2 = "/api/v2/projects"
P = "/api/v1/projects"


def _detail(api: Api, pid: str, jid: str) -> dict:
    return api.get(f"{V2}/{pid}/jobs/{jid}")


def _item(api: Api, pid: str, jid: str) -> dict:
    return _detail(api, pid, jid)["items"][0]


def _upload(api: Api, pid: str, color: tuple[int, int, int] = (200, 80, 40)) -> str:
    r = api.c.post(f"{P}/{pid}/references:upload", files={"file": ("ref.png", png_bytes(color=color))},
                   headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()["artifact_id"]


def _one_job(api: Api, pid: str, key: str, **kw: Any) -> tuple[str, dict]:
    jid = _job(api, pid, "concept", ["Tavern"], key, candidate_count=3, **kw)
    return jid, _item(api, pid, jid)


def _enhance(api: Api, pid: str, jid: str, key: str) -> dict:
    item = _item(api, pid, jid)
    api.post(f"{V2}/{pid}/jobs/{jid}:enhance", {"item_ids": [item["id"]], "idempotency_key": key})
    api.wait_ops()
    return _item(api, pid, jid)


def _confirm(api: Api, pid: str, jid: str, key: str, wait: bool = True) -> dict:
    item = _item(api, pid, jid)
    out = api.post(f"{V2}/{pid}/jobs/{jid}:confirm-and-generate", {"idempotency_key": key, "items": [
        {"item_id": item["id"], "prompt_revision_id": item["current_prompt"],
         "expected_item_revision": item["revision"]}]})
    if wait:
        api.wait_ops()
    return out


def _edit(api: Api, pid: str, jid: str, text: str) -> dict:
    item = _item(api, pid, jid)
    out = api.post(f"{V2}/{pid}/jobs/{jid}:edit-prompts", {"items": [
        {"item_id": item["id"], "expected_item_revision": item["revision"], "description": text}]})
    assert out["results"][0]["ok"], out
    return _item(api, pid, jid)


def _approve(api: Api, pid: str, jid: str, rnd: dict, idx: int, key: str) -> dict:
    item = _item(api, pid, jid)
    c = rnd["candidates"][idx]
    res = api.post(f"{V2}/{pid}/jobs/{jid}:approve-candidates", {"idempotency_key": key, "items": [{
        "item_id": item["id"], "expected_item_revision": item["revision"],
        "candidate_set_id": rnd["candidate_set_id"], "candidate_id": c["id"], "image_sha256": c["sha256"],
        "prompt_revision_id": rnd["prompt_revision_id"], "qa_evaluation_id": (c["qa"] or {}).get("id"),
        "override_qa": True}]})
    assert res["results"][0]["ok"], res
    return _item(api, pid, jid)


def _to_r1(api: Api, pid: str, key: str) -> tuple[str, dict]:
    jid, _ = _one_job(api, pid, key)
    api.post(f"{V2}/{pid}/jobs/{jid}:run", {"idempotency_key": f"{key}-run-key"})
    api.wait_ops()
    _confirm(api, pid, jid, f"{key}-confirm-1")
    return jid, _item(api, pid, jid)


def test_rounds_keep_every_set_and_approve_from_any_round(make_api) -> None:
    api = make_api()
    pid = _setup(api)
    jid, item = _to_r1(api, pid, "rounds-one")
    r1 = item["rounds"][0]
    assert len(item["rounds"]) == 1 and r1["number"] == 1 and len(r1["candidates"]) == 3
    assert all(c["qa"] is not None for c in r1["candidates"]) and item["approved"] is None
    item = _approve(api, pid, jid, r1, 1, "rounds-approve-1")
    assert item["approved"] == {"candidate_set_id": r1["candidate_set_id"], "candidate_id": r1["candidates"][1]["id"],
                                "round": 1}
    first_decision = item["approval"]
    # a new prompt is the next round; the approval of R1 survives it
    assert item["legal"]["edit_prompt"] and item["legal"]["confirm"] and item["legal"]["regenerate"]
    _edit(api, pid, jid, "a harbour at dusk")
    _confirm(api, pid, jid, "rounds-confirm-2")
    item = _item(api, pid, jid)
    assert [r["number"] for r in item["rounds"]] == [1, 2] and item["current_set"] == item["rounds"][1]["candidate_set_id"]
    assert item["approval"] == first_decision and item["approved"]["round"] == 1
    r1, r2 = item["rounds"]
    assert r1["prompt_revision_id"] != r2["prompt_revision_id"] and r2["prompt"]["origin"] == "edited"
    assert all(c["qa"] is not None for c in r1["candidates"])  # QA of R1 comes from qa_history
    assert list(item["qa_history"]) == [r1["candidate_set_id"]] and len(item["candidate_set"]["candidates"]) == 3
    # approve R2#1: replaces the approval with a new decision
    item = _approve(api, pid, jid, r2, 0, "rounds-approve-2")
    assert item["approval"] != first_decision and item["approved"]["round"] == 2
    assert first_decision in item["decisions"]
    # and R1 again (historic QA + the prompt of THAT round)
    item = _approve(api, pid, jid, item["rounds"][0], 2, "rounds-approve-3")
    assert item["approved"]["round"] == 1 and item["approved"]["candidate_id"] == r1["candidates"][2]["id"]
    assert len(item["decisions"]) == 3


def test_approval_from_history_is_still_exactly_bound(make_api) -> None:
    api = make_api()
    pid = _setup(api)
    jid, item = _to_r1(api, pid, "rounds-two")
    r1 = item["rounds"][0]
    _edit(api, pid, jid, "another idea")
    _confirm(api, pid, jid, "rounds-2-confirm-2")
    item = _item(api, pid, jid)
    c = r1["candidates"][0]
    base = {"item_id": item["id"], "expected_item_revision": item["revision"], "candidate_set_id": r1[
        "candidate_set_id"], "candidate_id": c["id"], "image_sha256": c["sha256"],
        "prompt_revision_id": r1["prompt_revision_id"], "qa_evaluation_id": c["qa"]["id"], "override_qa": True}
    for i, (field, value, code) in enumerate([
            ("image_sha256", "0" * 64, "sha_mismatch"),
            ("prompt_revision_id", item["rounds"][1]["prompt_revision_id"], "stale_prompt"),
            ("qa_evaluation_id", item["rounds"][1]["candidates"][0]["qa"]["id"], "stale_qa"),
            ("candidate_set_id", "cs_0000000000000000", "stale_set")]):
        res = api.post(f"{V2}/{pid}/jobs/{jid}:approve-candidates", {
            "idempotency_key": f"rounds-bad-{i}", "items": [{**base, field: value}]})
        assert res["results"][0]["code"] == code, res


def test_build_survives_new_round(make_api) -> None:
    api = make_api()
    pid = _setup(api)
    jid, item = _to_r1(api, pid, "rounds-three")
    item = _approve(api, pid, jid, item["rounds"][0], 0, "rounds-3-approve")
    api.post(f"{V2}/{pid}/jobs/{jid}:build-approved", {"idempotency_key": "rounds-3-build", "items": [
        {"item_id": item["id"], "approval_id": item["approval"], "expected_item_revision": item["revision"]}]})
    api.wait_ops()
    built = _item(api, pid, jid)
    assert built["build"]["result"] == "valid"
    _edit(api, pid, jid, "one more time")
    _confirm(api, pid, jid, "rounds-3-confirm-2")
    after = _item(api, pid, jid)
    assert len(after["rounds"]) == 2
    assert after["current_build"] == built["current_build"] and after["approval"] == built["approval"]
    assert after["build"]["result"] == "valid"


def test_regenerate_adds_a_round_and_keeps_approval(make_api) -> None:
    api = make_api()
    pid = _setup(api)
    jid, item = _to_r1(api, pid, "rounds-four")
    item = _approve(api, pid, jid, item["rounds"][0], 0, "rounds-4-approve")
    api.post(f"{V2}/{pid}/jobs/{jid}:regenerate", {"idempotency_key": "rounds-4-regen-1", "items": [
        {"item_id": item["id"], "expected_item_revision": item["revision"]}]})
    api.wait_ops()
    after = _item(api, pid, jid)
    assert len(after["rounds"]) == 2 and after["approval"] == item["approval"]
    seeds = [c["seed"] for r in after["rounds"] for c in r["candidates"]]
    assert len(set(seeds)) == 6  # same prompt, new seeds


def test_reference_crud_limits_and_busy(make_api) -> None:
    api = make_api(coordinator=False)
    pid = _setup(api)
    jid, item = _one_job(api, pid, "refs-crud-1")
    base = f"{V2}/{pid}/jobs/{jid}/items/{item['id']}"
    art = _upload(api, pid)
    out = api.post(f"{base}:add-reference", {"artifact_id": art, "note": "the roof shape", "label": "roof",
                                             "crop": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5},
                                             "expected_item_revision": item["revision"]})
    ref = out["references"][0]
    assert out["references_revision"] == 1 and out["revision"] == item["revision"] + 1
    assert ref["id"].startswith("jrf_") and ref["origin"] == "upload" and ref["artifact_id"] == art and len(ref["sha256"]) == 64
    bad = api.raw("POST", f"{base}:add-reference", json={"artifact_id": art, "expected_item_revision": out["revision"],
                                                       "crop": {"x": 0.6, "y": 0, "w": 0.5, "h": 0.5}})
    assert bad.status_code == 422
    for crop in ({"x": 0, "y": 0, "w": 0, "h": 1}, {"x": 0, "y": 0.9, "w": 0.5, "h": 0.2}):
        assert api.raw("POST", f"{base}:add-reference", json={
            "artifact_id": art, "expected_item_revision": out["revision"], "crop": crop}).status_code == 422
    unknown = api.raw("POST", f"{base}:add-reference", json={"artifact_id": "art_0000000000000000",
                                                           "expected_item_revision": out["revision"]})
    assert unknown.status_code == 422
    stale = api.raw("POST", f"{base}:add-reference", json={"artifact_id": art, "expected_item_revision": 1})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_item"
    upd = api.raw("PATCH", f"{base}:update-reference", json={"reference_id": ref["id"], "note": "new note",
                                                            "expected_item_revision": out["revision"]})
    assert upd.status_code == 200, upd.text
    body = upd.json()
    assert body["references"][0]["note"] == "new note" and body["references"][0]["crop"]["w"] == 0.5
    assert body["references_revision"] == 2
    cleared = api.raw("PATCH", f"{base}:update-reference", json={"reference_id": ref["id"], "crop": None,
                                                                "expected_item_revision": body["revision"]}).json()
    assert cleared["references"][0]["crop"] is None and cleared["references"][0]["note"] == "new note"
    rev = cleared["revision"]
    for n in range(3):  # fill to the limit of 4
        rev = api.post(f"{base}:add-reference", {"artifact_id": _upload(api, pid, (n, 9, 9)),
                                                 "expected_item_revision": rev})["revision"]
    fifth = api.raw("POST", f"{base}:add-reference", json={"artifact_id": art, "expected_item_revision": rev})
    assert fifth.status_code == 422 and fifth.json()["error"]["code"] == "too_many_references"
    gone = api.post(f"{base}:remove-reference", {"reference_id": ref["id"], "expected_item_revision": rev})
    assert len(gone["references"]) == 3 and gone["references_revision"] == 7
    missing = api.raw("POST", f"{base}:remove-reference", json={"reference_id": ref["id"],
                                                               "expected_item_revision": gone["revision"]})
    assert missing.status_code == 404
    view = _item(api, pid, jid)
    assert len(view["references"]) == 3 and view["references_revision"] == 7
    preset = api.raw("PATCH", f"{base}:set-preset", json={"preset": "creative", "expected_item_revision": view[
        "revision"]})
    assert preset.status_code == 200 and preset.json()["references_revision"] == 8
    assert _item(api, pid, jid)["enhance_preset"] == "creative"
    assert api.raw("PATCH", f"{base}:set-preset", json={"preset": "wild", "expected_item_revision": 1}).status_code in (
        400, 422)


def test_reference_change_refused_while_generating(make_api) -> None:
    api = make_api(coordinator=False)  # the generate task stays queued: generation is "running"
    pid = _setup(api)
    jid, item = _one_job(api, pid, "refs-busy-1")
    item = _edit(api, pid, jid, "a tavern")
    _confirm(api, pid, jid, "refs-busy-confirm", wait=False)
    item = _item(api, pid, jid)
    base = f"{V2}/{pid}/jobs/{jid}/items/{item['id']}"
    for method, action, body in (("POST", "add-reference", {"artifact_id": _upload(api, pid)}),
                                 ("PATCH", "set-preset", {"preset": "creative"})):
        r = api.raw(method, f"{base}:{action}", json={**body, "expected_item_revision": item["revision"]})
        assert r.status_code == 409 and r.json()["error"]["code"] == "busy", r.text


def test_vg05_reference_change_makes_confirmation_stale_until_reenhanced_or_edited(make_api) -> None:
    api = make_api()
    pid = _setup(api)
    jid, item = _one_job(api, pid, "refs-stale-1")
    item = _enhance(api, pid, jid, "refs-stale-1-enhance-1")
    assert item["prompt"]["bindings"]["references_revision"] == 0 and item["prompt_stale"] is False
    base = f"{V2}/{pid}/jobs/{jid}/items/{item['id']}"
    api.post(f"{base}:add-reference", {"artifact_id": _upload(api, pid), "note": "warm lantern light",
                                       "expected_item_revision": item["revision"]})
    item = _item(api, pid, jid)
    assert item["prompt_stale"] is True
    res = _confirm(api, pid, jid, "refs-stale-1-confirm-1")
    assert res["results"][0]["code"] == "stale_instruction_confirmation" and res["operations"] == []
    # re-enhancing binds the new references (the reference note reaches the enhancer's cues)
    item = _enhance(api, pid, jid, "refs-stale-1-enhance-2")
    b = item["prompt"]["bindings"]
    assert b["references_revision"] == 1 and len(b["reference_ids"]) == 1 and item["prompt_stale"] is False
    assert b["reference_cues"] == [{"index": 0, "cue": "warm lantern light"}]
    assert _confirm(api, pid, jid, "refs-stale-1-confirm-2")["results"][0]["ok"]
    # a hand edit is also an explicit answer to changed references (after another change)
    jid2, item2 = _one_job(api, pid, "refs-stale-2")
    item2 = _enhance(api, pid, jid2, "refs-stale-2-enhance-1")
    api.post(f"{V2}/{pid}/jobs/{jid2}/items/{item2['id']}:add-reference", {
        "artifact_id": _upload(api, pid), "expected_item_revision": item2["revision"]})
    assert _confirm(api, pid, jid2, "refs-stale-2-confirm-1")["results"][0]["code"] == "stale_instruction_confirmation"
    _edit(api, pid, jid2, "a tavern with a lantern")
    assert _confirm(api, pid, jid2, "refs-stale-2-confirm-2")["results"][0]["ok"]


def test_create_job_with_references_and_presets(make_api) -> None:
    api = make_api()
    pid = _setup(api)
    a = _upload(api, pid)
    out = api.post(f"{V2}/{pid}/jobs", {
        "title": "with refs", "category_id": "concept", "idempotency_key": "create-refs-1", "candidate_count": 2,
        "enhance_preset": "creative", "items": [
            {"name": "A", "brief": "a mill", "references": [{"artifact_id": a, "note": "stone wheel", "label": "wheel"}]},
            {"name": "B", "brief": "a bridge", "enhance_preset": "conservative"}]})
    jid = out["job"]["id"]
    items = _detail(api, pid, jid)["items"]
    assert [i["enhance_preset"] for i in items] == ["creative", "conservative"]
    assert items[0]["references_revision"] == 1 and items[1]["references_revision"] == 0
    (ref,) = items[0]["references"]
    assert ref["artifact_id"] == a and ref["note"] == "stone wheel" and ref["origin"] == "upload"
    too_many = api.raw("POST", f"{V2}/{pid}/jobs", json={
        "title": "x", "category_id": "concept", "idempotency_key": "create-refs-2", "items": [
            {"name": "A", "references": [{"artifact_id": a}] * 5}]})
    assert too_many.status_code == 422 and too_many.json()["error"]["code"] == "too_many_references"
    api.post(f"{V2}/{pid}/jobs/{jid}:enhance", {"item_ids": [i["id"] for i in items], "idempotency_key": "enhance-refs-1"})
    api.wait_ops()
    done = _detail(api, pid, jid)["items"]
    assert done[0]["prompt"]["bindings"]["preset"] == "creative" and done[0]["prompt"]["bindings"]["additions"]
    assert done[1]["prompt"]["bindings"]["additions"] == []
    assert done[0]["prompt"]["bindings"]["reference_cues"] == [{"index": 0, "cue": "stone wheel"}]


def test_library_reference_resolves_image_role(make_api) -> None:
    from tests.contract.test_api_library import _import

    api = make_api(coordinator=False)
    pid = _setup(api)
    src = _import(api, pid, "Tile", png_bytes(), "tile.png")
    jid, item = _one_job(api, pid, "refs-lib-1")
    out = api.post(f"{V2}/{pid}/jobs/{jid}/items/{item['id']}:add-reference", {
        "library": {"asset_id": src["asset_id"], "version_id": src["version_id"]}, "note": "colour palette",
        "expected_item_revision": item["revision"]})
    ref = out["references"][0]
    assert ref["origin"] == "library" and ref["library"]["asset_id"] == src["asset_id"]
