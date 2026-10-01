"""Candidate QA as three grouped stages: masks (GPU1 BiRefNet), VLM checks (GPU1 VLM), finalize (CPU metrics +
policy). Mask and VLM passes coalesce candidates of many Jobs into one residency each instead of alternating
models per candidate. Advisory only: QA never approves, and missing checks are `unavailable`, never a pass."""
from __future__ import annotations

from typing import Any

from assetstudio_core.canonical import now_iso, sha256_json
from assetstudio_core.domain import JobItem, QaEvaluation
from assetstudio_core.ids import derived_id
from assetstudio_core.qa import METRICS, CheckResult, QaRule, QaRuleset, evaluate_policy, parse_vlm_answers
from assetstudio_processing import metrics
from assetstudio_processing.images import load_rgb_array

from ...adapters.base import EngineRejected
from ...services.records import load_cset, load_item, load_job, load_prompt, mutate_item, qa_key
from ..runner import TaskEnv
from .qa_compare import compare_checks, compare_rules, unavailable


def reserved_colours(snap: dict[str, Any]) -> list[tuple[str, float]]:
    """Reserved palette colours that are NOT permitted in this item's kind/category scope."""
    style = snap.get("style") or {}
    kind, chain = snap["recipe"]["kind"], set(snap.get("category_chain") or [])
    return [(c["hex"].lower(), float(c["tolerance_delta_e"])) for c in style.get("palette", [])
            if c.get("reserved") and kind not in c.get("allowed_kinds", [])
            and not chain & set(c.get("allowed_categories", []))]


def rules_for(snap: dict[str, Any]) -> tuple[QaRuleset, list[QaRule], list[QaRule]]:
    ruleset = QaRuleset.model_validate(snap["qa_ruleset"]) if snap.get("qa_ruleset") else QaRuleset()
    rules = [r for r in ruleset.rules if r.stage == "candidate"]
    return ruleset, rules, [r for r in rules if r.enabled]


def qa_needs(snap: dict[str, Any]) -> tuple[bool, bool]:
    """(needs masks, needs VLM) for this item's frozen ruleset."""
    _, _, enabled = rules_for(snap)
    needs_mask = any(r.metric and "mask" in METRICS[r.metric].requires for r in enabled) or any(
        r.metric == "palette_reserved" for r in enabled)
    return needs_mask, any(r.source == "vlm" for r in enabled)


def _current(env: TaskEnv) -> tuple[JobItem, Any] | None:
    t = env.task
    item, _ = load_item(env.ctx.store, t.job_id, t.item_id)
    if item.current_set != t.inputs["candidate_set_id"]:
        return None
    return item, load_cset(env.ctx.store, t.job_id, t.inputs["candidate_set_id"])


def mask(env: TaskEnv) -> dict[str, Any]:
    cur = _current(env)
    if cur is None:
        return {"skipped": "candidate set superseded"}
    _, cset = cur
    aux, store = env.aux, env.ctx.store
    masks, errors = {}, {}
    for c in cset.candidates:
        env.check_cancel()
        try:
            assert aux is not None
            with env.call(c.id):
                res = aux.cutout(image=store.artifact_bytes(c.artifact_id), epoch=env.epoch("aux"),
                                 execution_id=derived_id("att", env.task.id, c.id))
        except EngineRejected as e:
            errors[c.id] = f"segmentation rejected the image: {e}"[:300]
            continue
        art = store.register_artifact(res["mask_png"], "candidate_mask", "image/png", lineage=[c.artifact_id],
                                      retention="candidate", source={"model": res.get("meta", {})},
                                      artifact_id=env.output_id("art", env.task.id, c.id, call=c.id))
        masks[c.id] = art.id
    return {"masks": masks, "errors": errors, "model": "birefnet"}


def _vlm(env: TaskEnv, vlm_rules: list[QaRule], image: bytes, context: str, cid: str) -> list[CheckResult]:
    aux = env.aux
    assert aux is not None
    try:
        with env.call(cid):
            res = aux.qa(image=image, questions=[(r.id, r.question or "") for r in vlm_rules], context=context,
                         epoch=env.epoch("aux"), execution_id=derived_id("att", env.task.id, cid))
    except EngineRejected as e:
        return [CheckResult(rule_id=r.id, source="vlm", severity=r.severity, result="unavailable",
                            reason=f"VLM rejected the request: {e}"[:300], evaluator="aux.vlm") for r in vlm_rules]
    answers = parse_vlm_answers(res.get("checks"), [r.id for r in vlm_rules])
    model = str((res.get("meta") or {}).get("model", "vlm"))
    reasons = res.get("reasons") if isinstance(res.get("reasons"), list) else []
    out = []
    for r in vlm_rules:
        a = answers[r.id]
        if isinstance(a, bool):
            out.append(CheckResult(rule_id=r.id, source="vlm", severity=r.severity, result="pass" if a else "fail",
                                   observed=a, reason="" if a else "; ".join(str(x) for x in reasons[:2])[:300],
                                   evaluator=model))
        else:
            out.append(CheckResult(rule_id=r.id, source="vlm", severity=r.severity, result="unavailable", reason=a,
                                   evaluator=model))
    return out


def qa_vlm(env: TaskEnv) -> dict[str, Any]:
    cur = _current(env)
    if cur is None:
        return {"skipped": "candidate set superseded"}
    item, cset = cur
    snap = env.ctx.store.read_snapshot(item.snapshot_sha)
    _, _, enabled = rules_for(snap)
    vlm_rules = [r for r in enabled if r.source == "vlm"]
    prompt = load_prompt(env.ctx.store, env.task.job_id, cset.prompt_revision_id)
    out = {}
    for c in cset.candidates:
        env.check_cancel()
        results = _vlm(env, vlm_rules, env.ctx.store.artifact_bytes(c.artifact_id), prompt.positive, c.id)
        out[c.id] = [r.model_dump() for r in results]
    return {"vlm": out}


def _dep_results(env: TaskEnv) -> dict[str, Any]:
    """Results of the mask/VLM tasks this finalize depends on (absent or unsuccessful -> unavailable checks)."""
    out: dict[str, Any] = {}
    for dep in env.studio.journal.tasks.many(env.task.deps):
        out[dep.stage] = dep.result if dep.state == "succeeded" and dep.result else {
            "unavailable": f"{dep.stage} {dep.state}: {(dep.error or {}).get('message', '')}"[:300]}
    return out


def _compare_results(checks: list[Any], res: dict[str, Any], cid: str, no_service: str) -> list[CheckResult]:
    """Advisory comparison checks of one candidate; anything not computed is `unavailable` (never a pass)."""
    if not checks:
        return []
    got = (res.get("compare") or {}).get(cid)
    if got:
        return [CheckResult.model_validate(x) for x in got]
    return unavailable(checks, res.get("unavailable") or no_service or "comparison check not computed")


def qa_finalize(env: TaskEnv) -> dict[str, Any]:
    cur = _current(env)
    if cur is None:
        return {"skipped": "candidate set superseded"}
    item, cset = cur
    store, t = env.ctx.store, env.task
    snap = store.read_snapshot(item.snapshot_sha)
    ruleset, rules, enabled = rules_for(snap)
    deps = _dep_results(env)
    masks_res, vlm_res, cmp_res = deps.get("mask", {}), deps.get("qa_vlm", {}), deps.get("qa_compare", {})
    job, _ = load_job(store, t.job_id)
    cmp_checks = compare_checks(job, item, snap, store)
    cmp_rules = compare_rules(cmp_checks)
    no_service = "" if env.aux is not None else "no VLM/segmentation service configured"
    reserved = reserved_colours(snap)
    evaluated: dict[str, str] = {}
    for c in cset.candidates:
        results: list[CheckResult] = []
        for r in enabled:
            if r.source != "vlm":
                continue
            got = next((x for x in (vlm_res.get("vlm") or {}).get(c.id, []) if x["rule_id"] == r.id), None)
            results.append(CheckResult.model_validate(got) if got else CheckResult(
                rule_id=r.id, source="vlm", severity=r.severity, result="unavailable",
                reason=vlm_res.get("unavailable") or no_service or "VLM check not computed", evaluator="aux.vlm"))
        mask_id = (masks_res.get("masks") or {}).get(c.id)
        mask_arr = metrics.mask_array(store.artifact_bytes(mask_id)) if mask_id else None
        mask_err = (masks_res.get("errors") or {}).get(c.id) or masks_res.get("unavailable") or no_service
        for r in enabled:
            if r.source == "vlm":
                continue
            if r.metric in ("mask_margin", "mask_fill", "mask_single_blob") and mask_arr is None:
                results.append(metrics.unavailable(r, mask_err or "mask not computed"))
                continue
            rgb = load_rgb_array(store.artifact_bytes(c.artifact_id)) if r.metric == "palette_reserved" else None
            results.append(metrics.evaluate_metric(r, image_size=(c.width, c.height), mask=mask_arr, rgb=rgb,
                                                   reserved=reserved))
        results += _compare_results(cmp_checks, cmp_res, c.id, no_service)
        policy = evaluate_policy(rules + cmp_rules, results, ruleset.policy)
        qid = derived_id("qa", t.id, c.id)
        if store.repo.stat_object(qa_key(t.job_id, qid)) is None:
            store.create(qa_key(t.job_id, qid), QaEvaluation(
                id=qid, item_id=item.id, candidate_set_id=cset.id, candidate_id=c.id, image_sha256=c.sha256,
                ruleset=snap.get("qa_ruleset"), ruleset_sha=sha256_json(snap["qa_ruleset"]) if snap.get(
                    "qa_ruleset") else None, results=[x.model_dump() for x in results], policy=policy,
                evaluators={"mask_artifact_id": mask_id, "mask_task": next(
                    (d for d in t.deps if (dt := env.studio.journal.tasks.get(d)) and dt.stage == "mask"), None),
                    "simulated": bool(env.aux and env.aux.simulated),
                    **({"compare": cmp_res.get("inputs") or {}} if cmp_checks else {})},
                evaluated_at=now_iso(), op_id=t.id))
        evaluated[c.id] = qid

    def apply(x: JobItem) -> None:
        if x.current_set == cset.id:
            x.qa = {**x.qa, **evaluated}
    mutate_item(env.studio, env.ctx, t.job_id, item.id, apply)
    return {"evaluated": len(evaluated)}
