"""Batch lifecycle contracts with the SIMULATED engine (B01-B07, P01, P03, Q04). Not GPU evidence."""
from __future__ import annotations

from typing import Any

from tests.conftest import Api, new_project

CONCEPT = {"categories": [{"id": "concept", "slug": "concept", "label": "Concept",
                           "defaults": {"kind": "concept_art", "naming": "concept_{name}"}},
                          {"id": "props", "slug": "props", "label": "Props", "defaults": {"kind": "model3d"}}]}


def setup_project(api: Api) -> str:
    pid = new_project(api)
    cfg = api.get(f"/api/v1/projects/{pid}/config")["config"]
    cfg.update(CONCEPT)
    r = api.raw("PATCH", f"/api/v1/projects/{pid}/config", json={"expected_revision": 1, "config": cfg})
    assert r.status_code == 200, r.text
    return pid


def create(api: Api, pid: str, names: list[str], key: str, category: str = "concept", **kw: Any) -> dict:
    body = {"title": "t", "category_id": category, "idempotency_key": key,
            "items": [{"name": n, "brief": f"brief for {n}"} for n in names], **kw}
    return api.post(f"/api/v1/projects/{pid}/batches", body)


def detail(api: Api, pid: str, bid: str) -> dict:
    return api.get(f"/api/v1/projects/{pid}/batches/{bid}")


def confirm_all(api: Api, pid: str, bid: str, key: str) -> dict:
    d = detail(api, pid, bid)
    return api.post(f"/api/v1/projects/{pid}/batches/{bid}:confirm-and-generate", {
        "idempotency_key": key, "items": [{"item_id": i["id"], "prompt_revision_id": i["current_prompt"],
                                           "expected_item_revision": i["revision"]} for i in d["items"]]})


def approve(api: Api, pid: str, bid: str, item: dict, index: int, key: str, override: bool = True) -> dict:
    c = item["candidate_set"]["candidates"][index]
    return api.post(f"/api/v1/projects/{pid}/batches/{bid}:approve-candidates", {"idempotency_key": key, "items": [{
        "item_id": item["id"], "expected_item_revision": item["revision"],
        "candidate_set_id": item["candidate_set"]["id"], "candidate_id": c["id"], "image_sha256": c["sha256"],
        "prompt_revision_id": item["candidate_set"]["prompt_revision_id"],
        "qa_evaluation_id": c["qa"]["id"] if c["qa"] else None, "override_qa": override}]})


def test_single_item_full_lifecycle(api: Api) -> None:
    pid = setup_project(api)
    out = create(api, pid, ["Tavern interior"], "batch-single-1")
    bid = out["batch"]["id"]
    assert out["enhance"]["operation"]["kind"] == "enhance"
    api.wait_ops()
    d = detail(api, pid, bid)
    item = d["items"][0]
    assert item["prompt"]["origin"] == "enhanced" and item["prompt_confirmed"] is None
    assert item["candidate_set"] is None  # B03: nothing generated before confirmation
    confirm_all(api, pid, bid, "confirm-single-1")
    api.wait_ops()
    item = detail(api, pid, bid)["items"][0]
    cands = item["candidate_set"]["candidates"]
    assert len(cands) == 4 and all(c["qa"] is not None for c in cands)
    assert len({c["seed"] for c in cands}) == 4
    res = approve(api, pid, bid, item, 1, "approve-single-1")
    assert res["results"][0]["ok"], res
    item = detail(api, pid, bid)["items"][0]
    b = api.post(f"/api/v1/projects/{pid}/batches/{bid}:build-approved", {
        "idempotency_key": "build-single-1", "items": [
        {"item_id": item["id"], "approval_id": item["approval"], "expected_item_revision": item["revision"]}]})
    assert b["operation"]["kind"] == "build"
    api.wait_ops()
    item = detail(api, pid, bid)["items"][0]
    assert item["build"]["result"] == "valid"
    assert api.get(f"/api/v1/projects/{pid}/assets")["total"] == 0  # P01: build alone publishes nothing
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:accept-builds", {"idempotency_key": "accept-single-1", "items": [
        {"item_id": item["id"], "build_run_id": item["current_build"], "expected_item_revision": item["revision"]}]})
    prev = api.get(f"/api/v1/projects/{pid}/batches/{bid}/publish-preview")["items"]
    assert prev[0]["name_id"] == "concept_tavern_interior" and prev[0]["new_asset"]
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:publish", {"idempotency_key": "publish-single-1", "items": [
        {"item_id": p["item_id"], "build_run_id": p["build_run_id"], "expected_item_revision": p[
            "expected_item_revision"]} for p in prev]})
    api.wait_ops()
    lib = api.get(f"/api/v1/projects/{pid}/assets")
    assert lib["total"] == 1 and lib["items"][0]["origin"] == "generated"
    asset = api.get(f"/api/v1/projects/{pid}/assets/{lib['items'][0]['asset_id']}")
    v = asset["shown_version"]
    assert v["sources"]["candidate_id"] == cands[1]["id"] and v["licence"]["status"] == "not_cleared"  # simulated
    assert {f["role"] for f in asset["files"]} == {"image", "preview"}
    assert detail(api, pid, bid)["items"][0]["stage"]["stage"] == "done"
    # retrying the same publish command is a no-op (one version)
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:publish", {"idempotency_key": "publish-single-1", "items": [
        {"item_id": p["item_id"], "build_run_id": p["build_run_id"], "expected_item_revision": p[
            "expected_item_revision"]} for p in prev]})
    api.wait_ops()
    assert len(api.get(f"/api/v1/projects/{pid}/assets/{lib['items'][0]['asset_id']}/versions")["versions"]) == 1


def test_seven_items_partial_decisions_and_regeneration(api: Api) -> None:
    pid = setup_project(api)
    bid = create(api, pid, [f"Item {i}" for i in range(7)], "batch-seven-1")["batch"]["id"]
    api.wait_ops()
    confirm_all(api, pid, bid, "confirm-seven-1")
    api.wait_ops()
    items = detail(api, pid, bid)["items"]
    seeds = [c["seed"] for i in items for c in i["candidate_set"]["candidates"]]
    assert len(seeds) == 28 and len(set(seeds)) == 28  # B02
    for n in range(4):
        assert approve(api, pid, bid, items[n], n % 4, f"approve-seven-{n}")["results"][0]["ok"]
    regen_item = detail(api, pid, bid)["items"][4]
    old_set = regen_item["candidate_set"]["id"]
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:mark-regenerate", {"mark": True, "items": [
        {"item_id": regen_item["id"], "expected_item_revision": regen_item["revision"]}]})
    items = detail(api, pid, bid)["items"]
    build_items = [{"item_id": i["id"], "approval_id": i["approval"], "expected_item_revision": i["revision"]}
                   for i in items if i["approval"]]
    assert len(build_items) == 4
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:build-approved", {"idempotency_key": "build-seven-1",
                                                                      "items": build_items})
    api.wait_ops()
    d = detail(api, pid, bid)
    assert d["counts"]["built"] == 4 and d["counts"]["regenerate"] == 1  # B04
    assert [i["build"] is not None for i in d["items"]] == [True] * 4 + [False] * 3
    target = d["items"][4]
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:regenerate", {"idempotency_key": "regen-seven-1", "items": [
        {"item_id": target["id"], "expected_item_revision": target["revision"], "description": "a new idea"}]})
    api.wait_ops()
    d2 = detail(api, pid, bid)
    new = d2["items"][4]
    assert new["candidate_set"]["id"] != old_set and len(new["candidate_sets"]) == 2  # B05
    assert new["prompt"]["description"] == "a new idea" and new["approval"] is None
    for before, after in zip(d["items"][:4] + d["items"][5:], d2["items"][:4] + d2["items"][5:], strict=True):
        assert before["candidate_set"]["id"] == after["candidate_set"]["id"]  # siblings untouched


def test_stale_bindings_rejected(api: Api) -> None:
    pid = setup_project(api)
    bid = create(api, pid, ["A"], "batch-stale-1")["batch"]["id"]
    api.wait_ops()
    d = detail(api, pid, bid)
    item = d["items"][0]
    # B03: confirming a revision that is not current is refused
    r = api.raw("POST", f"/api/v1/projects/{pid}/batches/{bid}:confirm-and-generate", json={
        "idempotency_key": "confirm-stale-1", "items": [{"item_id": item["id"], "prompt_revision_id": "prm_x",
                                                         "expected_item_revision": item["revision"]}]})
    assert r.json()["results"][0]["code"] == "stale_prompt" and r.json()["operations"] == []
    confirm_all(api, pid, bid, "confirm-stale-2")
    api.wait_ops()
    item = detail(api, pid, bid)["items"][0]
    c = item["candidate_set"]["candidates"][0]
    base = {"item_id": item["id"], "expected_item_revision": item["revision"],
            "candidate_set_id": item["candidate_set"]["id"], "candidate_id": c["id"], "image_sha256": c["sha256"],
            "prompt_revision_id": item["candidate_set"]["prompt_revision_id"], "qa_evaluation_id": c["qa"]["id"],
            "override_qa": True}
    for i, (field, value, code) in enumerate([("image_sha256", "0" * 64, "sha_mismatch"),
                                              ("expected_item_revision", 1, "stale_item"),
                                              ("qa_evaluation_id", None, "stale_qa"),
                                              ("candidate_set_id", "cs_0000000000000000", "stale_set")]):
        res = api.post(f"/api/v1/projects/{pid}/batches/{bid}:approve-candidates", {
            "idempotency_key": f"approve-stale-{i}", "items": [{**base, field: value}]})
        assert res["results"][0]["code"] == code, res
    # prompts are locked once candidates exist
    r = api.post(f"/api/v1/projects/{pid}/batches/{bid}:edit-prompts", {"items": [
        {"item_id": item["id"], "expected_item_revision": item["revision"], "description": "x"}]})
    assert r["results"][0]["code"] == "prompts_locked"


def test_override_required_for_non_recommended(api: Api) -> None:
    pid = setup_project(api)
    api.studio.aux.vlm_answers = {"no_text": "false"}  # string, not a boolean: must be unavailable (Q03)
    bid = create(api, pid, ["A"], "batch-ovr-1")["batch"]["id"]
    api.wait_ops()
    confirm_all(api, pid, bid, "confirm-ovr-1")
    api.wait_ops()
    item = detail(api, pid, bid)["items"][0]
    qa = item["candidate_set"]["candidates"][0]["qa"]
    assert qa["status"] == "unverified" and qa["results"][0]["result"] == "unavailable"
    res = approve(api, pid, bid, item, 0, "approve-ovr-1", override=False)
    assert res["results"][0]["code"] == "override_required"
    res = approve(api, pid, bid, item, 0, "approve-ovr-2", override=True)
    assert res["results"][0]["ok"]
    dec = detail(api, pid, bid)["items"][0]["approval_detail"]
    assert dec["override_qa"] is True and dec["missing_checks"] == ["no_text"]


def test_preview_best_and_mixed_kinds_and_shot_claims(api: Api) -> None:
    pid = setup_project(api)
    api.studio.aux.vlm_answers = {"no_text": True}
    bid = create(api, pid, ["A", "B"], "batch-best-1")["batch"]["id"]
    api.wait_ops()
    confirm_all(api, pid, bid, "confirm-best-1")
    api.wait_ops()
    prev = api.post(f"/api/v1/projects/{pid}/batches/{bid}:preview-best", {})
    assert len(prev["proposals"]) == 2 and all(p["index"] == 0 for p in prev["proposals"])
    assert detail(api, pid, bid)["counts"]["approved"] == 0  # preview never approves
    # mixed kinds are refused with a split suggestion
    r = api.raw("POST", f"/api/v1/projects/{pid}/batches", json={
        "title": "mix", "idempotency_key": "batch-mix-1", "items": [
            {"name": "x", "category_id": "concept"}, {"name": "y", "category_id": "props"}]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "mixed_kinds"
    shots = api.raw("PUT", f"/api/v1/projects/{pid}/shot-list", json={"expected_revision": 0, "items": [
        {"name": "Well", "category_id": "concept", "brief": "stone well"}]}).json()
    sid = shots["items"][0]["id"]
    create(api, pid, ["Well"], "batch-shot-1", items=[{"name": "Well", "shot_id": sid}])
    r = api.raw("POST", f"/api/v1/projects/{pid}/batches", json={
        "title": "again", "category_id": "concept", "idempotency_key": "batch-shot-2",
        "items": [{"name": "Well", "shot_id": sid}]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "shot_claimed"
    statuses = {s["id"]: s["status"] for s in api.get(f"/api/v1/projects/{pid}/shot-list")["items"]}
    assert statuses[sid] == "in_batch"
    planned = api.get(f"/api/v1/projects/{pid}/assets")
    assert planned["total"] == 0 and planned["planned_total"] == 1


def test_3d_build_blocked_with_reason(api: Api) -> None:
    pid = setup_project(api)
    bid = create(api, pid, ["Crate"], "batch-3d-1", category="props")["batch"]["id"]
    d = detail(api, pid, bid)
    assert d["recipe"]["build_available"] is False and "DINOv3" in d["recipe"]["build_blocked_reason"]
    r = api.raw("POST", f"/api/v1/projects/{pid}/batches/{bid}:build-approved", json={
        "idempotency_key": "build-3d-1", "items": [{"item_id": d["items"][0]["id"], "approval_id": "dec_x",
                                                    "expected_item_revision": 1}]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "build_unavailable"
