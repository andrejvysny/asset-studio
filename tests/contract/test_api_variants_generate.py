"""Variants Phase E-a: source-conditioned candidate generation (VG01-VG04). SIMULATED engines only: contract
evidence for conditioning/wiring, never image-model quality."""
from __future__ import annotations

import hashlib
from typing import Any

from assetstudio_server.adapters.base import ImageEditRequest
from assetstudio_server.services.edit_templates import EDIT_NEGATIVE, FIXED_SENTENCE
from assetstudio_server.services.variant_jobs import load_plan

from tests.conftest import Api, new_project
from tests.contract.test_api_library import _glb, _import
from tests.contract.test_api_variants import _create, _draft, _prepare, _rows

V2 = "/api/v2/projects"


def _variant_jobs(api: Api, rows: int = 1) -> tuple[str, dict, dict]:
    pid = new_project(api)
    src = _import(api, pid, "Crate", _glb(), "crate.glb")
    d = _draft(api, pid, src, "image_edit_reconstruct", "draft-vg-1", rows=_rows(rows), requested_variants=rows)
    d = _prepare(api, pid, d)
    return pid, src, _create(api, pid, d, "jobs-vg-1")


def _item(api: Api, pid: str, jid: str) -> dict:
    return api.get(f"{V2}/{pid}/jobs/{jid}")["items"][0]


def _run_and_confirm(api: Api, pid: str, jid: str, key: str) -> dict:
    api.post(f"{V2}/{pid}/jobs/{jid}:run", {"idempotency_key": f"run-{key}-1"})
    api.wait_ops()
    item = _item(api, pid, jid)
    assert item["current_prompt"], item["stage"]
    api.post(f"{V2}/{pid}/jobs/{jid}:confirm-and-generate", {"idempotency_key": f"confirm-{key}-1", "items": [
        {"item_id": item["id"], "prompt_revision_id": item["current_prompt"],
         "expected_item_revision": item["revision"]}]})
    api.wait_ops()
    return _item(api, pid, jid)


def _edit_requests(api: Api) -> list[ImageEditRequest]:
    return [j["req"] for j in api.studio.engine.jobs.values() if isinstance(j["req"], ImageEditRequest)]  # type: ignore[union-attr]


def test_vg01_variant_job_enhances_in_edit_mode_and_conditions_on_plan_reference(make_api) -> None:
    api = make_api()
    pid, src, out = _variant_jobs(api)
    jid = out["job_ids"][0]
    plan = load_plan(api.studio.registry.get(pid), out["plan_id"])
    primary = next(r for r in plan.references if r.role == "primary")
    item = _run_and_confirm(api, pid, jid, "vg01")
    prompt = item["prompt"]
    assert prompt["positive"].startswith(FIXED_SENTENCE) and prompt["negative"] == EDIT_NEGATIVE
    b = prompt["bindings"]
    assert b["mode"] == "edit" and b["plan_id"] == plan.id and b["plan_sha256"] == plan.sha256
    assert b["primary_reference_sha256"] == primary.sha256 and b["reference_set_id"] == plan.reference_set_id
    assert "same three-quarter camera view" in prompt["template"]  # model3d edit constraints
    reqs = _edit_requests(api)
    assert len(reqs) == 4 and item["candidate_set"] is not None
    for r in reqs:  # every slot: the plan's primary reference bytes, verified against the frozen digest
        assert r.prepared_input_sha256 == primary.sha256 == hashlib.sha256(r.image).hexdigest()
        assert r.source_sha256 != r.prepared_input_sha256 and r.steps == 40 and r.cfg == 4.0
    cands = item["candidate_set"]["candidates"]
    assert len(cands) == 4 and all(c["sha256"] != primary.sha256 for c in cands)
    gen = item["candidate_set"]["generation"]
    assert gen["mode"] == "image_edit" and gen["models"] == ["qwen_image_edit_2511", "qwen_image_2512"]
    assert gen["conditioning"]["sha256"] == primary.sha256 and gen["style_lora"] is None
    ctx = api.studio.registry.get(pid)
    art = ctx.store.artifact(cands[0]["artifact_id"])
    assert art.source["conditioning"]["artifact_id"] == primary.artifact_id
    assert art.source["receipt"]["workflow"] == "fake.image_edit" and art.source["simulated"] is True


def test_vg02_residency_names_edit_model_without_row_or_source_ids(make_api) -> None:
    api = make_api()
    pid, src, out = _variant_jobs(api)
    jid = out["job_ids"][0]
    _run_and_confirm(api, pid, jid, "vg02")
    gen = [t for t in api.studio.journal.tasks.list(project_id=pid) if t.stage == "generate"]
    assert len(gen) == 1
    res = gen[0].residency
    row_id = api.get(f"{V2}/{pid}/jobs/{jid}")["variant"]["row_id"]
    assert "qwen_image_edit_2511" in res and res.endswith("|edit")
    for ident in (row_id, src["asset_id"], src["version_id"], jid, out["plan_id"]):
        assert ident not in res


def test_vg04_second_variant_job_conditions_on_plan_reference_not_sibling_candidates(make_api) -> None:
    api = make_api()
    pid, src, out = _variant_jobs(api, rows=2)
    plan = load_plan(api.studio.registry.get(pid), out["plan_id"])
    primary = next(r for r in plan.references if r.role == "primary")
    first, second = out["job_ids"]
    a = _run_and_confirm(api, pid, first, "vg04a")
    b = _run_and_confirm(api, pid, second, "vg04b")
    produced = {c["sha256"] for it in (a, b) for c in it["candidate_set"]["candidates"]}
    reqs = _edit_requests(api)
    assert len(reqs) == 8
    for r in reqs:
        assert r.prepared_input_sha256 == primary.sha256 and hashlib.sha256(r.image).hexdigest() == primary.sha256
        assert r.prepared_input_sha256 not in produced
    assert len({r.seed for r in reqs}) == 8  # independent seeds per Job and slot


def test_vg03_no_edit_engine_blocks_generation_before_any_task(make_api) -> None:
    api = make_api()
    pid, src, out = _variant_jobs(api)
    jid = out["job_ids"][0]
    api.post(f"{V2}/{pid}/jobs/{jid}:run", {"idempotency_key": "run-vg03-1"})
    api.wait_ops()
    item = _item(api, pid, jid)
    api.studio.engine.supports = lambda kind: kind == "t2i"  # type: ignore[union-attr,method-assign]
    res: Any = api.post(f"{V2}/{pid}/jobs/{jid}:confirm-and-generate", {
        "idempotency_key": "confirm-vg03-1", "items": [{"item_id": item["id"], "expected_item_revision": item[
            "revision"], "prompt_revision_id": item["current_prompt"]}]})
    assert res["results"][0]["code"] == "editing_model_unavailable" and res["operations"] == []
    assert not [t for t in api.studio.journal.tasks.list(project_id=pid) if t.stage == "generate"]


def test_vg06_source_integrity_failure_fails_the_item_without_engine_calls(make_api, monkeypatch) -> None:
    from assetstudio_server.coordinator.stages import generate as gen_stage
    from assetstudio_server.services.variant_gen import SourceIntegrityError

    def corrupt(*_: Any) -> bytes:
        raise SourceIntegrityError("source reference three_quarter differs from the frozen plan")

    api = make_api()
    pid, src, out = _variant_jobs(api)
    jid = out["job_ids"][0]
    api.post(f"{V2}/{pid}/jobs/{jid}:run", {"idempotency_key": "run-vg06-1"})
    api.wait_ops()
    monkeypatch.setattr(gen_stage, "primary_bytes", corrupt)
    item = _item(api, pid, jid)
    api.post(f"{V2}/{pid}/jobs/{jid}:confirm-and-generate", {"idempotency_key": "confirm-vg06-1", "items": [
        {"item_id": item["id"], "prompt_revision_id": item["current_prompt"],
         "expected_item_revision": item["revision"]}]})
    api.wait_ops()
    (task,) = [t for t in api.studio.journal.tasks.list(project_id=pid) if t.stage == "generate"]
    assert task.state == "failed" and task.error["code"] == "source_integrity_failed"
    assert not _edit_requests(api) and _item(api, pid, jid)["current_set"] is None
