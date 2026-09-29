"""Candidate QA pass (GPU1 aux): masks first, then VLM, then deterministic metrics. Advisory; never approves."""
from __future__ import annotations

from typing import Any

from assetstudio_core.canonical import now_iso, sha256_json
from assetstudio_core.domain import BatchItem, QaEvaluation
from assetstudio_core.ids import derived_id
from assetstudio_core.qa import METRICS, CheckResult, QaRule, QaRuleset, evaluate_policy, parse_vlm_answers
from assetstudio_processing import metrics
from assetstudio_processing.images import load_rgb_array

from ..adapters.base import EngineRejected, EngineUnavailable
from ..services.records import load_cset, load_item, load_prompt, mutate_item, qa_key, set_task
from .runner import TaskEnv
from .tasks_prompt import _items_error


def reserved_colours(snap: dict[str, Any]) -> list[tuple[str, float]]:
    """Reserved palette colours that are NOT permitted in this item's kind/category scope."""
    style = snap.get("style") or {}
    kind, chain = snap["recipe"]["kind"], set(snap.get("category_chain") or [])
    return [(c["hex"].lower(), float(c["tolerance_delta_e"])) for c in style.get("palette", [])
            if c.get("reserved") and kind not in c.get("allowed_kinds", [])
            and not chain & set(c.get("allowed_categories", []))]


def _vlm_results(env: TaskEnv, rules: list[QaRule], image: bytes, context: str) -> list[CheckResult]:
    vlm_rules = [r for r in rules if r.source == "vlm"]
    if not vlm_rules:
        return []
    aux = env.studio.aux

    def unavailable(reason: str) -> list[CheckResult]:
        return [CheckResult(rule_id=r.id, source="vlm", severity=r.severity, result="unavailable", reason=reason,
                            evaluator="aux.vlm") for r in vlm_rules]
    if aux is None:
        return unavailable("no VLM service configured")
    try:
        res = aux.qa(image=image, questions=[(r.id, r.question or "") for r in vlm_rules], context=context)
    except (EngineUnavailable, EngineRejected) as e:
        return unavailable(f"VLM unavailable: {e}"[:300])
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


def qa(env: TaskEnv) -> dict[str, Any]:
    p = env.op.payload
    store = env.ctx.store
    item, _ = load_item(store, p["batch_id"], p["item_id"])
    if item.current_set != p["candidate_set_id"]:
        return {"skipped": "candidate set superseded"}
    mutate_item(env.studio, env.ctx, p["batch_id"], item.id, lambda x: set_task(x, "qa", env.op.id, "running"))
    cset = load_cset(store, p["batch_id"], p["candidate_set_id"])
    snap = store.read_snapshot(item.snapshot_sha)
    ruleset = QaRuleset.model_validate(snap["qa_ruleset"]) if snap.get("qa_ruleset") else QaRuleset()
    rules = [r for r in ruleset.rules if r.stage == "candidate"]
    enabled = [r for r in rules if r.enabled]
    prompt = load_prompt(store, p["batch_id"], cset.prompt_revision_id)
    needs_mask = any(r.metric and "mask" in METRICS[r.metric].requires for r in enabled) or any(
        r.metric == "palette_reserved" for r in enabled)
    if any(r.source == "vlm" for r in enabled) or needs_mask:
        if env.studio.aux is not None:
            env.studio.lanes["gpu1"].acquire("aux")
    images = {c.id: store.artifact_bytes(c.artifact_id) for c in cset.candidates}
    masks: dict[str, Any] = {}
    mask_errors: dict[str, str] = {}
    if needs_mask:  # grouped: all segmentation first, then VLM (one model focus at a time)
        for c in cset.candidates:
            env.check_cancel()
            try:
                if env.studio.aux is None:
                    raise EngineUnavailable("no segmentation service configured")
                res = env.studio.aux.cutout(image=images[c.id])
                art = store.register_artifact(res["mask_png"], "candidate_mask", "image/png", lineage=[c.artifact_id],
                                              retention="candidate", source={"model": res.get("meta", {})})
                masks[c.id] = (metrics.mask_array(res["mask_png"]), art.id)
            except (EngineUnavailable, EngineRejected) as e:
                mask_errors[c.id] = f"segmentation unavailable: {e}"[:300]
    reserved = reserved_colours(snap)
    evaluated: dict[str, str] = {}
    for c in cset.candidates:
        env.check_cancel()
        results = _vlm_results(env, enabled, images[c.id], prompt.positive)
        mask = masks.get(c.id, (None, None))[0]
        for r in enabled:
            if r.source == "vlm":
                continue
            if r.metric in ("mask_margin", "mask_fill", "mask_single_blob") and mask is None:
                results.append(metrics.unavailable(r, mask_errors.get(c.id, "mask not computed")))
                continue
            rgb = load_rgb_array(images[c.id]) if r.metric == "palette_reserved" else None
            results.append(metrics.evaluate_metric(r, image_size=(c.width, c.height), mask=mask, rgb=rgb,
                                                   reserved=reserved))
        policy = evaluate_policy(rules, results, ruleset.policy)
        qid = derived_id("qa", env.op.id, c.id)
        if store.repo.stat_object(qa_key(p["batch_id"], qid)) is None:
            store.create(qa_key(p["batch_id"], qid), QaEvaluation(
                id=qid, item_id=item.id, candidate_set_id=cset.id, candidate_id=c.id, image_sha256=c.sha256,
                ruleset=snap.get("qa_ruleset"), ruleset_sha=sha256_json(snap["qa_ruleset"]) if snap.get(
                    "qa_ruleset") else None, results=[x.model_dump() for x in results], policy=policy,
                evaluators={"mask_artifact_id": masks.get(c.id, (None, None))[1],
                            "simulated": bool(env.studio.aux and env.studio.aux.simulated)},
                evaluated_at=now_iso(), op_id=env.op.id))
        evaluated[c.id] = qid

    def apply(x: BatchItem) -> None:
        if x.current_set == cset.id:
            x.qa = {**x.qa, **evaluated}
        set_task(x, "qa", env.op.id, "succeeded")
    mutate_item(env.studio, env.ctx, p["batch_id"], item.id, apply)
    return {"evaluated": len(evaluated)}


def qa_error(env: TaskEnv, state: str, message: str) -> None:
    _items_error(env, "qa", state, message, [env.op.payload["item_id"]])
