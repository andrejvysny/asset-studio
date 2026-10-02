"""Prompt variants (#51): one enhanced prompt per preview slot, one preview per prompt, per-variant retry.
SIMULATED engines only: contract evidence, never model-quality evidence."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from assetstudio_server.adapters.base import EngineRejected

from tests.contract.test_api_rounds_refs import V2, _confirm, _edit, _enhance, _item, _upload
from tests.contract.test_jobs_batches import _job, _setup, _studio


def _variant_job(api: Any, pid: str, key: str, count: int = 4) -> str:
    return _job(api, pid, "concept", ["Tavern"], f"{key}-job", candidate_count=count)


def test_four_distinct_prompts_one_preview_each(make_api, tmp_path: Path) -> None:
    api, _, aux, _ = _studio(make_api, tmp_path)
    pid = _setup(api)
    jid = _variant_job(api, pid, "pv-one")
    item = _enhance(api, pid, jid, "pv-one-enhance")
    assert aux.calls.count("enhance") == 4  # enhancement runs once per preview slot
    variants = item["prompt_variants"]
    assert len(variants) == 4 and item["prompt"]["id"] == variants[0]["id"] == item["current_prompt"]
    texts = [v["description"] for v in variants]
    assert len(set(texts)) == 4, texts  # intentionally different, not lexical copies of one prompt
    assert [v["bindings"]["variant_index"] for v in variants] == [0, 1, 2, 3]
    assert all(v["bindings"]["variant_count"] == 4 and v["origin"] == "enhanced" for v in variants)
    assert item["prompt_confirmed"] is None

    _confirm(api, pid, jid, "pv-one-confirm")
    item = _item(api, pid, jid)
    rnd = item["rounds"][0]
    cands = item["candidate_set"]["candidates"]
    assert len(cands) == 4 and [c["prompt_revision_id"] for c in cands] == [v["id"] for v in variants]
    assert set(rnd["prompts"]) == {v["id"] for v in variants}  # the UI can reveal each preview's prompt
    for c, v in zip(cands, variants, strict=True):
        assert rnd["prompts"][c["prompt_revision_id"]]["positive"] == v["positive"]
    assert item["candidate_set"]["prompt_revision_id"] == variants[0]["id"]  # the set keeps its primary prompt


def test_single_candidate_and_edit_jobs_keep_one_prompt(make_api, tmp_path: Path) -> None:
    api, _, aux, _ = _studio(make_api, tmp_path)
    pid = _setup(api)
    jid = _variant_job(api, pid, "pv-single", count=1)
    item = _enhance(api, pid, jid, "pv-single-enhance")
    assert aux.calls.count("enhance") == 1 and item["prompt_variants"] == []
    _confirm(api, pid, jid, "pv-single-confirm")
    cand = _item(api, pid, jid)["candidate_set"]["candidates"][0]
    assert cand["prompt_revision_id"] == item["current_prompt"]


def test_editing_one_variant_changes_only_that_preview_prompt(make_api, tmp_path: Path) -> None:
    api, _, _, _ = _studio(make_api, tmp_path)
    pid = _setup(api)
    jid = _variant_job(api, pid, "pv-edit")
    item = _enhance(api, pid, jid, "pv-edit-enhance")
    before = [v["id"] for v in item["prompt_variants"]]
    out = api.post(f"{V2}/{pid}/jobs/{jid}:edit-prompts", {"items": [{
        "item_id": item["id"], "expected_item_revision": item["revision"], "variant_index": 2,
        "description": "a tavern seen from above, lanterns lit"}]})
    assert out["results"][0]["ok"], out
    item = _item(api, pid, jid)
    after = [v["id"] for v in item["prompt_variants"]]
    assert after[:2] == before[:2] and after[3] == before[3] and after[2] != before[2]
    assert item["current_prompt"] == before[0]
    assert item["prompt_variants"][2]["description"] == "a tavern seen from above, lanterns lit"
    assert item["prompt_variants"][2]["bindings"]["variant_index"] == 2
    bad = api.post(f"{V2}/{pid}/jobs/{jid}:edit-prompts", {"items": [{
        "item_id": item["id"], "expected_item_revision": item["revision"], "variant_index": 7,
        "description": "x"}]})
    assert bad["results"][0]["code"] == "unknown_variant"
    _confirm(api, pid, jid, "pv-edit-confirm")
    cands = _item(api, pid, jid)["candidate_set"]["candidates"]
    assert cands[2]["prompt_revision_id"] == after[2] and cands[0]["prompt_revision_id"] == before[0]


def test_editing_the_primary_rebinds_stale_variants_to_new_references(make_api, tmp_path: Path) -> None:
    api, _, _, _ = _studio(make_api, tmp_path)
    pid = _setup(api)
    jid = _variant_job(api, pid, "pv-refs", count=3)
    item = _enhance(api, pid, jid, "pv-refs-enhance")
    api.post(f"{V2}/{pid}/jobs/{jid}/items/{item['id']}:add-reference", {
        "artifact_id": _upload(api, pid), "expected_item_revision": item["revision"]})
    assert _confirm(api, pid, jid, "pv-refs-c1")["results"][0]["code"] == "stale_instruction_confirmation"
    item = _edit(api, pid, jid, "a tavern with a lantern")
    revs = {v["bindings"]["references_revision"] for v in item["prompt_variants"]}
    assert revs == {1} and len(item["prompt_variants"]) == 3
    assert [v["description"] for v in item["prompt_variants"][1:]] != [item["prompt_variants"][0]["description"]] * 2
    assert _confirm(api, pid, jid, "pv-refs-c2")["results"][0]["ok"]


def test_a_failed_enhancement_retries_only_the_missing_variants(make_api, tmp_path: Path) -> None:
    api, _, aux, _ = _studio(make_api, tmp_path)
    pid = _setup(api)
    real = aux.enhance
    state = {"n": 0}

    def flaky(**kw: Any) -> dict[str, Any]:
        state["n"] += 1
        if state["n"] == 3:  # the third variant's call is rejected once
            raise EngineRejected("simulated enhancer rejection")
        return real(**kw)
    aux.enhance = flaky  # type: ignore[method-assign]
    jid = _variant_job(api, pid, "pv-retry")
    item = _item(api, pid, jid)
    api.post(f"{V2}/{pid}/jobs/{jid}:enhance", {"item_ids": [item["id"]], "idempotency_key": "pv-retry-enhance"})
    api.wait_ops()
    item = _item(api, pid, jid)
    assert item["prompt_variants"] == [] and item["tasks"]["enhance"]["state"] == "failed"
    assert state["n"] == 3  # variants 1-2 done, 3 rejected, 4 never started
    api.post(f"/api/v2/tasks/{item['tasks']['enhance']['op_id']}:retry", {})
    api.wait_ops()
    item = _item(api, pid, jid)
    assert len(item["prompt_variants"]) == 4
    assert state["n"] == 3 + 2  # only the rejected variant and the one after it called the enhancer again


def test_one_failed_preview_slot_does_not_restart_the_others(make_api, tmp_path: Path) -> None:
    api, engine, _, _ = _studio(make_api, tmp_path)
    pid = _setup(api)
    jid = _variant_job(api, pid, "pv-slot")
    item = _enhance(api, pid, jid, "pv-slot-enhance")
    real_submit = engine.submit
    seen: list[str] = []

    def submit(req: Any) -> Any:
        seen.append(req.positive)
        if len(seen) == 2:
            engine.fail_prompts.add(req.prompt_id)
        return real_submit(req)
    engine.submit = submit  # type: ignore[method-assign]
    _confirm(api, pid, jid, "pv-slot-confirm")
    item = _item(api, pid, jid)
    cands = item["candidate_set"]["candidates"]
    assert len(cands) == 3 and item["candidate_set"]["requested"] == 4  # one slot failed, three previews exist
    prompts = [v["id"] for v in item["prompt_variants"]]
    assert {c["prompt_revision_id"] for c in cands} <= set(prompts) and len({c["prompt_revision_id"] for c in cands}) == 3
    assert len(set(seen)) == 4  # every slot was generated from its own prompt text
