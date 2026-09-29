"""Variants Phase E-b: advisory variant/reference QA (aux compare), selection diversity, exact final sizing and
build retry modes (VQ01-VQ06). SIMULATED engines only: contract evidence, never model-quality evidence."""
from __future__ import annotations

import json
from typing import Any

from assetstudio_core.domain import JobItem
from assetstudio_processing.transforms import inspect_static_glb
from assetstudio_server.adapters.base import EngineRejected
from assetstudio_server.adapters.fake import FakeAux, FakeEngine, FakeWorker3d
from assetstudio_server.services.diversity import ALL_PAIRS_MAX, SHORTLIST, shortlist_pairs
from assetstudio_server.services.records import mutate_item, qa_key

from tests.conftest import Api, new_project, png_bytes
from tests.contract.test_api_batches import approve, confirm_all, create, detail, setup_project
from tests.contract.test_api_library import _glb, _import
from tests.contract.test_api_variants import _create, _draft, _png_src, _prepare, _rows
from tests.contract.test_jobs_batches import _job, _setup

P = "/api/v1/projects"
V2 = "/api/v2/projects"


def _item(api: Api, pid: str, jid: str) -> dict:
    return api.get(f"{V2}/{pid}/jobs/{jid}")["items"][0]


def _variant_jobs(api: Api, pid: str, n: int, key: str, method: str = "image_edit", **row: Any) -> dict:
    if method == "image_edit":
        src = _png_src(api, pid, f"Tile{key}")
    else:
        src = _import(api, pid, f"Crate{key}", _glb(), "crate.glb")
    d = _draft(api, pid, src, method, f"draft-{key}", rows=_rows(n, **row), requested_variants=n)
    out = _create(api, pid, _prepare(api, pid, d), f"jobs-{key}")
    return {**out, "source": src}


def _generate(api: Api, pid: str, jid: str, key: str) -> dict:
    """Enhance (run), confirm the prompt, generate, QA: leaves the item waiting for approval."""
    api.post(f"{V2}/{pid}/jobs/{jid}:run", {"idempotency_key": f"run-{key}"})
    api.wait_ops()
    it = _item(api, pid, jid)
    api.post(f"{V2}/{pid}/jobs/{jid}:confirm-and-generate", {"idempotency_key": f"conf-{key}", "items": [{
        "item_id": it["id"], "expected_item_revision": it["revision"], "prompt_revision_id": it["current_prompt"]}]})
    api.wait_ops()
    return _item(api, pid, jid)


def _approve(api: Api, pid: str, jid: str, item: dict, index: int, key: str, override: bool = True,
             status: int | tuple[int, ...] = 200) -> dict:
    c = item["candidate_set"]["candidates"][index]
    return api.post(f"{V2}/{pid}/jobs/{jid}:approve-candidates", {"idempotency_key": key, "items": [{
        "item_id": item["id"], "expected_item_revision": item["revision"],
        "candidate_set_id": item["candidate_set"]["id"], "candidate_id": c["id"], "image_sha256": c["sha256"],
        "prompt_revision_id": item["candidate_set"]["prompt_revision_id"],
        "qa_evaluation_id": c["qa"]["id"] if c["qa"] else None, "override_qa": override,
        "override_reason": "test" if override else None}]}, status=status)


def _results(cand: dict) -> dict[str, dict]:
    return {r["rule_id"]: r for r in cand["qa"]["results"]}


def test_vq01_variant_candidates_get_comparison_checks(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    out = _variant_jobs(api, pid, 1, "vq01")
    jid = out["job_ids"][0]
    item = _generate(api, pid, jid, "vq01")
    assert "compare" in api.studio.aux.calls  # type: ignore[union-attr]
    for cand in item["candidate_set"]["candidates"]:
        res = _results(cand)
        for rid in ("variant_resemblance", "variant_change", "variant_single_object"):
            assert res[rid]["result"] == "pass" and res[rid]["reason"] and res[rid]["source"] == "vlm_compare"
            assert res[rid]["severity"] == ("major" if rid == "variant_change" else "minor") and res[rid]["evaluator"] == "simulated"
        assert res["variant_style"]["result"] == "not_applicable"  # no project style guide text
        assert cand["qa"]["status"] == "recommended"
    ctx = api.studio.registry.get(pid)
    qa = json.loads(ctx.store.repo.read_object(qa_key(jid, item["candidate_set"]["candidates"][0]["qa"]["id"])).data)
    plan = json.loads(ctx.store.repo.read_object(f"variant-plans/{out['plan_id']}.json").data)
    primary = next(r for r in plan["references"] if r["role"] == "primary")
    assert qa["evaluators"]["compare"] == {"source_reference_sha": primary["sha256"], "reference_shas": {}}


def test_vq02_aux_rejection_is_unavailable_and_unverified(make_api) -> None:
    api = make_api()
    aux: FakeAux = api.studio.aux  # type: ignore[assignment]

    def refuse(**_: Any) -> dict:
        raise EngineRejected("simulated overload")
    aux.compare = refuse  # type: ignore[method-assign]
    pid = new_project(api)
    jid = _variant_jobs(api, pid, 1, "vq02")["job_ids"][0]
    item = _generate(api, pid, jid, "vq02")
    for cand in item["candidate_set"]["candidates"]:
        res = _results(cand)
        assert {res[r]["result"] for r in ("variant_resemblance", "variant_change", "variant_single_object")} == {
            "unavailable"}
        assert cand["qa"]["status"] == "unverified"  # missing comparison checks are never a pass
        assert {"variant_resemblance", "variant_change"} <= set(cand["qa"]["policy"]["unavailable"])


def test_vq02b_no_aux_configured_is_unverified(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    jid = _variant_jobs(api, pid, 1, "vq02b")["job_ids"][0]
    api.post(f"{V2}/{pid}/jobs/{jid}:run", {"idempotency_key": "run-vq02b"})
    api.wait_ops()
    it = _item(api, pid, jid)
    api.studio.aux = None  # type: ignore[assignment]  # the service disappears after enhancement
    api.post(f"{V2}/{pid}/jobs/{jid}:confirm-and-generate", {"idempotency_key": "conf-vq02b", "items": [{
        "item_id": it["id"], "expected_item_revision": it["revision"], "prompt_revision_id": it["current_prompt"]}]})
    api.wait_ops()
    for cand in _item(api, pid, jid)["candidate_set"]["candidates"]:
        res = _results(cand)
        assert res["variant_change"]["result"] == "unavailable" and "no VLM" in res["variant_change"]["reason"]
        assert cand["qa"]["status"] == "unverified"


def test_vq03_advisory_failures_do_not_auto_reject(make_api) -> None:
    api = make_api()
    aux: FakeAux = api.studio.aux  # type: ignore[assignment]
    real = aux.compare

    def picky(**kw: Any) -> dict:
        res = real(**kw)
        res["checks"] = {k: (False if k in ("variant_resemblance", "variant_single_object") else v)
                         for k, v in res["checks"].items()}
        return res
    aux.compare = picky  # type: ignore[method-assign]
    pid = new_project(api)
    jid = _variant_jobs(api, pid, 1, "vq03")["job_ids"][0]
    item = _generate(api, pid, jid, "vq03")
    cand = item["candidate_set"]["candidates"][0]
    assert cand["qa"]["status"] == "not_recommended" and cand["qa"]["policy"]["failed_major"] == []
    r = _approve(api, pid, jid, item, 0, "appr-vq03-a", override=False)
    assert r["results"][0]["code"] == "override_required"
    ok = _approve(api, pid, jid, item, 0, "appr-vq03-b", override=True)
    assert ok["results"][0]["ok"]
    dec = _item(api, pid, jid)["approval_detail"]
    assert dec["override_qa"] and {"variant_resemblance", "variant_single_object"} <= set(dec["failed_checks"])


def test_vq06_change_check_fails_when_candidate_equals_source(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    out = _variant_jobs(api, pid, 1, "vq06")
    ctx = api.studio.registry.get(pid)
    plan = json.loads(ctx.store.repo.read_object(f"variant-plans/{out['plan_id']}.json").data)
    source = ctx.store.artifact_bytes(next(r for r in plan["references"] if r["role"] == "primary")["artifact_id"])
    engine: FakeEngine = api.studio.engine  # type: ignore[assignment]
    engine.fetch_image = lambda *_a, **_k: source  # type: ignore[method-assign]
    item = _generate(api, pid, out["job_ids"][0], "vq06")
    for cand in item["candidate_set"]["candidates"]:
        res = _results(cand)
        assert res["variant_change"]["result"] == "fail" and "identical" in res["variant_change"]["reason"]
        assert res["variant_resemblance"]["result"] == "pass"


def test_reference_qa_checks_per_item_reference(make_api) -> None:
    api = make_api()
    seen: list[list[tuple[str, str, int]]] = []
    aux: FakeAux = api.studio.aux  # type: ignore[assignment]
    real = aux.compare

    def spy(**kw: Any) -> dict:
        import io

        from PIL import Image
        seen.append([(lbl, note, Image.open(io.BytesIO(b)).size[0]) for b, lbl, note in kw["images"]])
        return real(**kw)
    aux.compare = spy  # type: ignore[method-assign]
    pid = _setup(api)
    jid = _job(api, pid, "concept", ["Tavern"], "job-refqa-1", candidate_count=2)
    ctx = api.studio.registry.get(pid)
    refs = []
    for n, (note, crop) in enumerate([("wooden door", {"x": 0, "y": 0, "w": 0.5, "h": 0.5}), ("", None)]):
        art = ctx.store.register_artifact(png_bytes(64, 64, (10 * n, 20, 30)), "reference", "image/png")
        refs.append({"id": f"ref{n}", "artifact_id": art.id, "sha256": art.sha256, "origin": "upload",
                     "note": note, "crop": crop, "label": f"r{n}"})
    item = _item(api, pid, jid)

    def attach(x: JobItem) -> None:
        x.references, x.references_revision = refs, 1
    mutate_item(api.studio, ctx, jid, item["id"], attach)
    item = _generate(api, pid, jid, "refqa")
    for cand in item["candidate_set"]["candidates"]:
        res = _results(cand)
        assert res["ref_0"]["result"] == "pass" and res["ref_1"]["result"] == "pass"
        assert "variant_change" not in res  # not a variant Job
    assert ("reference", "wooden door", 32) in seen[0]  # cropped to its box, note passed through
    assert ("reference", "", 64) in seen[1]


def test_shortlist_all_pairs_or_nearest(make_api) -> None:
    assert len(shortlist_pairs([i * 977 for i in range(ALL_PAIRS_MAX)])) == 66
    many = [(1 << i) - 1 for i in range(20)]  # 190 pairs; nearest by Hamming distance
    pairs = shortlist_pairs(many)
    assert len(pairs) == SHORTLIST and pairs == sorted(pairs)
    assert (0, 1) in pairs and (0, 19) not in pairs  # near neighbours are kept, far ones are dropped
    assert len(shortlist_pairs(many[:13])) == 78


def _approved_plan(api: Api, pid: str, n: int, key: str) -> dict:
    out = _variant_jobs(api, pid, n, key)
    items = {}
    for jid in out["job_ids"]:
        it = _generate(api, pid, jid, f"{key}-{jid[-4:]}")
        _approve(api, pid, jid, it, 0, f"appr-{key}-{jid[-4:]}")
        items[jid] = _item(api, pid, jid)
    return {**out, "items": items}


def _diversity(api: Api, pid: str, plan_id: str, key: str) -> dict:
    res = api.post(f"{P}/{pid}/variant-plans/{plan_id}:compare-selection", {"idempotency_key": key})
    api.wait_ops()
    return res


def test_diversity_report_full_coverage_and_stale_on_changed_selection(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    plan = _approved_plan(api, pid, 3, "div1")
    pl = plan["plan_id"]
    assert api.get(f"{P}/{pid}/variant-plans/{pl}/diversity")["status"] == "missing"
    res = _diversity(api, pid, pl, "cmp-div1-a")
    assert res["status"] == "queued" and res["coverage"] == "full" and res["pairs"] == 3 and res["selected"] == 3
    got = api.get(f"{P}/{pid}/variant-plans/{pl}/diversity")
    rep = got["report"]
    assert got["status"] == "current" and rep["coverage"] == "full" and rep["ruleset"] == "diversity.v1"
    assert len(rep["pairs"]) == 3 and rep["not_evaluated_pairs"] == 0 and rep["digest"] == got["selection_digest"]
    assert {p["result"] for p in rep["pairs"]} == {"pass"} and rep["evaluator"] == "simulated"
    assert set(got["jobs"].values()) == {"pass"} and set(got["jobs"]) == set(plan["job_ids"])
    assert rep["deterministic"]["exact_duplicates"] == [] and len(rep["deterministic"]["dhash_pairs"]) == 3
    again = api.post(f"{P}/{pid}/variant-plans/{pl}:compare-selection", {"idempotency_key": "cmp-div1-b"})
    assert again["status"] == "current" and again["task_id"] is None  # same selection: nothing to recompute
    jid = plan["job_ids"][0]
    it = plan["items"][jid]
    approvals_before = {j: i["approval"] for j, i in plan["items"].items()}
    _approve(api, pid, jid, it, 1, "appr-div1-change")
    changed = api.get(f"{P}/{pid}/variant-plans/{pl}/diversity")
    assert changed["status"] == "stale" and changed["report"]["id"] == rep["id"]
    assert changed["selection_digest"] != rep["digest"]
    assert _item(api, pid, plan["job_ids"][1])["approval"] == approvals_before[plan["job_ids"][1]]  # untouched


def test_diversity_flags_exact_duplicates_without_the_vlm(make_api) -> None:
    api = make_api()
    engine: FakeEngine = api.studio.engine  # type: ignore[assignment]
    same = png_bytes(64, 64, (90, 90, 90))
    engine.fetch_image = lambda *_a, **_k: same  # type: ignore[method-assign]
    pid = new_project(api)
    plan = _approved_plan(api, pid, 2, "div2")
    aux: FakeAux = api.studio.aux  # type: ignore[assignment]
    before = aux.calls.count("compare")
    _diversity(api, pid, plan["plan_id"], "cmp-div2-a")
    assert aux.calls.count("compare") == before  # identical bytes need no semantic look
    got = api.get(f"{P}/{pid}/variant-plans/{plan['plan_id']}/diversity")
    dups = got["report"]["deterministic"]["exact_duplicates"]
    assert len(dups) == 1 and set(dups[0]) == set(plan["job_ids"])
    assert set(got["jobs"].values()) == {"fail"} and got["report"]["pairs"][0]["result"] == "fail"


def test_diversity_needs_two_selections_and_excludes_direct_rows(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    out = _variant_jobs(api, pid, 2, "div3")
    r = api.raw("POST", f"{P}/{pid}/variant-plans/{out['plan_id']}:compare-selection",
                json={"idempotency_key": "cmp-div3-a"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "selection_too_small"


def _reconstruct(api: Api, pid: str, key: str, height: float | None) -> tuple[str, dict]:
    out = _variant_jobs(api, pid, 1, key, method="image_edit_reconstruct", final_height_m=height)
    jid = out["job_ids"][0]
    item = _generate(api, pid, jid, key)
    _approve(api, pid, jid, item, 0, f"appr-{key}")
    item = _item(api, pid, jid)
    api.post(f"{V2}/{pid}/jobs/{jid}:build-approved", {"idempotency_key": f"build-{key}", "items": [{
        "item_id": item["id"], "approval_id": item["approval"], "expected_item_revision": item["revision"]}]})
    api.wait_ops()
    return jid, _item(api, pid, jid)


def test_final_sizing_of_reconstructed_variant(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    jid, item = _reconstruct(api, pid, "size1", 3.0)
    build = item["build"]
    assert build["result"] == "valid", build["validation"]
    checks = {c["id"]: c for c in build["validation"]["checks"]}
    assert checks["final_height"]["ok"] and "final_height" in build["validation"]["required"]
    assert {"model", "model_unsized"} <= set(build["artifacts"]) and "size" in build["checkpoints"]
    store = api.studio.registry.get(pid).store
    sized = inspect_static_glb(store.artifact_bytes(build["artifacts"]["model"]))
    unsized = inspect_static_glb(store.artifact_bytes(build["artifacts"]["model_unsized"]))
    height = sized["bounds"]["max"][1] - sized["bounds"]["min"][1]
    assert abs(height - 3.0) < 1e-3 and abs(unsized["bounds"]["max"][1] - unsized["bounds"]["min"][1] - 3.0) > 0.1
    meta = json.loads(store.artifact_bytes(build["artifacts"]["meta"]))
    assert meta["sizing"]["height_ok"] is True and meta["sizing"]["transform"]["units_confirmed"] is True
    api.post(f"{V2}/{pid}/jobs/{jid}:accept-builds", {"idempotency_key": "acc-size1", "items": [{
        "item_id": item["id"], "build_run_id": item["current_build"], "expected_item_revision": item["revision"]}]})
    prev = api.get(f"{V2}/{pid}/jobs/{jid}/publish-preview")["items"]
    api.post(f"{V2}/{pid}/jobs/{jid}:publish", {"idempotency_key": "pub-size1", "items": [{
        "item_id": p["item_id"], "build_run_id": p["build_run_id"], "expected_item_revision":
        p["expected_item_revision"], "expected_current_version": p["current_version_id"]} for p in prev]})
    api.wait_ops()
    asset = api.get(f"{P}/{pid}/assets/{_item(api, pid, jid)['published']['asset_id']}")
    roles = {f["role"] for f in asset["files"]}
    assert "model" in roles and "model_unsized" not in roles
    assert asset["shown_version"]["sources"]["intermediates"]["model_unsized"] == build["artifacts"]["model_unsized"]


def test_no_sizing_without_a_row_height(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    _, item = _reconstruct(api, pid, "size2", None)
    assert item["build"]["result"] == "valid" and "model_unsized" not in item["build"]["artifacts"]
    assert "final_height" not in {c["id"] for c in item["build"]["validation"]["checks"]}


def _model_built(api: Api, key: str) -> tuple[str, str, dict]:
    pid = setup_project(api)
    bid = create(api, pid, ["Crate"], f"b3d-{key}-create", category="props", candidate_count=2)["batch"]["id"]
    api.wait_ops()
    confirm_all(api, pid, bid, f"c3d-{key}-conf")
    api.wait_ops()
    it = detail(api, pid, bid)["items"][0]
    assert approve(api, pid, bid, it, 0, f"a3d-{key}-appr")["results"][0]["ok"]
    return pid, bid, _build(api, pid, bid, f"first-{key}-build")


def _build(api: Api, pid: str, bid: str, key: str, **extra: Any) -> dict:
    it = detail(api, pid, bid)["items"][0]
    r = api.raw("POST", f"{P}/{pid}/batches/{bid}:build-approved", json={"idempotency_key": key, "items": [{
        "item_id": it["id"], "approval_id": it["approval"], "expected_item_revision": it["revision"], **extra}]})
    assert r.status_code in (200, 202), r.text
    api.wait_ops()
    return detail(api, pid, bid)["items"][0]


def _w3d(api: Api) -> FakeWorker3d:
    return api.studio.worker3d  # type: ignore[return-value]


def test_resample_uses_a_new_seed_and_samples_again(make_api) -> None:
    api = make_api()
    pid, bid, first = _model_built(api, "rsmp")
    assert first["build"]["result"] == "valid" and _w3d(api).calls.count("generate") == 1
    second = _build(api, pid, bid, "resample-rsmp", mode="resample")
    b = second["build"]
    assert b["id"] != first["build"]["id"] and b["result"] == "valid" and b["inputs"]["mode"] == "resample"
    assert _w3d(api).calls.count("generate") == 2  # sampled again
    assert set(b["checkpoints"]) >= {"segment", "sample", "bake"}
    assert b["checkpoints"]["segment"]["outputs"] == first["build"]["checkpoints"]["segment"]["outputs"]
    hist = {h["id"]: h for h in second["build_history"]}
    old, new = hist[first["build"]["id"]], hist[b["id"]]
    assert old["mode"] == "build" and new["mode"] == "resample" and new["seed"] != old["seed"]
    assert b["checkpoints"]["sample"]["settings"]["seed"] == new["seed"]


def test_rebuild_applies_overrides_and_keeps_the_sample(make_api) -> None:
    api = make_api()
    pid, bid, first = _model_built(api, "rbld")
    second = _build(api, pid, bid, "rebuild-rbld-1", mode="rebuild", overrides={"triangles": 3000, "texture_size": 1024})
    b = second["build"]
    assert b["result"] == "valid" and b["inputs"]["mode"] == "rebuild" and b["inputs"]["resumed_from"]
    assert b["checkpoints"]["bake"]["settings"]["decimation_target"] == 3000
    assert b["checkpoints"]["bake"]["settings"]["texture_size"] == 1024
    assert _w3d(api).calls.count("generate") == 1  # the sampled raw is reused
    assert second["build_history"][-1]["overrides"] == {"triangles": 3000, "texture_size": 1024}
    third = _build(api, pid, bid, "rebuild-rbld-2", mode="rebuild", overrides={"pipeline_type": "512"})
    assert third["build"]["result"] == "valid" and _w3d(api).calls.count("generate") == 2  # pipeline change resamples


def test_mode_validation(make_api) -> None:
    api = make_api()
    pid, bid, first = _model_built(api, "mval")
    it = detail(api, pid, bid)["items"][0]
    unit = {"item_id": it["id"], "approval_id": it["approval"], "expected_item_revision": it["revision"]}

    def post(**extra: Any) -> Any:
        return api.raw("POST", f"{P}/{pid}/batches/{bid}:build-approved",
                       json={"idempotency_key": f"mval-{len(extra)}-{sorted(extra.get('overrides', {}))}",
                             "items": [{**unit, **extra}]})
    assert post(mode="rebuild", overrides={"seed": 3}).status_code == 422
    assert post(mode="rebuild", overrides={"triangles": 10}).status_code == 422  # below the recipe minimum
    assert post(mode="rebuild").status_code == 422
    assert post(mode="retry", overrides={"triangles": 3000}).status_code == 422
    assert post(mode="bogus").status_code == 400  # request validation
    jid = _job(api, pid, "concept", ["Tavern"], "job-concept-mv", candidate_count=2)
    r = api.raw("POST", f"{V2}/{pid}/jobs/{jid}:build-approved", json={"idempotency_key": "mval-concept-1", "items": [
        {"item_id": "itm_x", "approval_id": "dec_x", "expected_item_revision": 1, "mode": "resample"}]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "mode_unsupported"


def test_retry_after_failed_bake_resumes_from_raw(make_api) -> None:
    api = make_api()
    pid = setup_project(api)
    bid = create(api, pid, ["Crate"], "b3d-rt-create", category="props", candidate_count=2)["batch"]["id"]
    api.wait_ops()
    confirm_all(api, pid, bid, "c3d-rt-conf")
    api.wait_ops()
    it = detail(api, pid, bid)["items"][0]
    approve(api, pid, bid, it, 0, "a3d-rt-appr")
    _w3d(api).fail_ops["export"] = "boom"
    failed = _build(api, pid, bid, "build-rt-1")
    assert failed["build"]["status"] == "failed" and "raw" in failed["build"]["artifacts"]
    retried = _build(api, pid, bid, "build-rt-2", mode="retry")
    b = retried["build"]
    assert b["result"] == "valid" and b["inputs"]["resumed_from"] == failed["build"]["id"]
    assert b["inputs"]["mode"] == "retry" and _w3d(api).calls.count("generate") == 1
