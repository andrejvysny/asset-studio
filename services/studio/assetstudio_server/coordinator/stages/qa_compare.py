"""Advisory comparison QA (aux /compare): variant candidates against their source, candidates against the item's
guidance references. Never approves anything; a check that could not run is `unavailable`, never a pass."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from assetstudio_core.domain import Job, JobItem
from assetstudio_core.ids import derived_id
from assetstudio_core.qa import CheckResult, QaRule
from assetstudio_processing.images import crop_png

from ...adapters.base import EngineRejected
from ...services.records import load_cset, load_item, load_job, load_prompt
from ..runner import TaskEnv

MAX_REFERENCES = 4
SOURCE_QUESTIONS = {
    "variant_resemblance": "Is the candidate recognisably the same kind of object as the source, sharing its "
                           "identity and material treatment?",
    "variant_single_object": "Is the candidate one complete isolated object (no collage, comparison panel, "
                             "duplicates or scene)?",
}


@dataclass(frozen=True)
class Check:
    id: str
    question: str
    group: str  # "source" (one call for all source checks) | "ref_<i>" (one call per reference)
    applicable: bool = True
    reference_id: str | None = None  # artifact of the reference image (item references)
    crop: Any = None
    note: str = ""


def _plan_of(env_ctx: Any, job: Job) -> Any:
    from ...services.variant_jobs import load_plan

    return load_plan(env_ctx, (job.variant or {})["plan_id"])


def is_generative_variant(job: Job) -> bool:
    return job.variant is not None and not job.direct


def compare_checks(job: Job, item: JobItem, snap: dict[str, Any]) -> list[Check]:
    """The deterministic list of comparison checks for this item (same answer at task and finalize time)."""
    checks: list[Check] = []
    if is_generative_variant(job):
        v = job.variant or {}
        guide = str((snap.get("style") or {}).get("guide") or "").strip()
        change = str(v.get("change_request") or "").strip() or "the requested change"
        checks += [Check("variant_resemblance", SOURCE_QUESTIONS["variant_resemblance"], "source"),
                   Check("variant_change", f"Compared with the source, does the candidate show this requested "
                         f"change: {change}? Changes of camera angle, background or colour alone do not count.",
                         "source"),
                   Check("variant_single_object", SOURCE_QUESTIONS["variant_single_object"], "source"),
                   Check("variant_style", f"Does the candidate match this project style: {guide}?", "source",
                         applicable=bool(guide))]
    for i, ref in enumerate(item.references[:MAX_REFERENCES]):
        note = str(ref.get("note") or "").strip()
        checks.append(Check(f"ref_{i}", "Does the candidate match what matters in the reference: "
                            f"{note or 'overall appearance'}?", f"ref_{i}", reference_id=ref.get("artifact_id"),
                            crop=ref.get("crop"), note=note))
    return checks


def compare_needed(job: Job, item: JobItem) -> bool:
    return is_generative_variant(job) or bool(item.references)


# A variant that does not show its requested change is not a verified variant (GPU acceptance published a source
# clone as "recommended" when this was minor). Major = not recommended; still overridable, never a hard block.
MAJOR_CHECKS = frozenset({"variant_change"})


def _severity(check_id: str) -> str:
    return "major" if check_id in MAJOR_CHECKS else "minor"


def compare_rules(checks: list[Check]) -> list[QaRule]:
    """Synthetic advisory rules: a failed comparison lowers the recommendation, never blocks approval."""
    return [QaRule(id=c.id, source="vlm", severity=_severity(c.id), question=c.question[:500])  # type: ignore[arg-type]
            for c in checks]


def _result(c: Check, result: str, reason: str, model: str, observed: Any = None) -> CheckResult:
    return CheckResult(rule_id=c.id, source="vlm_compare", severity=_severity(c.id),  # type: ignore[arg-type]
                       result=result,  # type: ignore[arg-type]
                       reason=reason[:300], observed=observed, evaluator=model)


def _answers(res: dict[str, Any], group: list[Check]) -> list[CheckResult]:
    model = str((res.get("meta") or {}).get("model", "vlm"))
    checks, reasons = res.get("checks"), res.get("reasons")
    checks = checks if isinstance(checks, dict) else {}
    reasons = reasons if isinstance(reasons, dict) else {}
    out = []
    for c in group:
        a, why = checks.get(c.id), str(reasons.get(c.id) or "")
        if isinstance(a, bool):
            out.append(_result(c, "pass" if a else "fail", why, model, observed=a))
        else:
            out.append(_result(c, "unavailable", why or f"VLM was unsure or gave no answer ({a!r})", model))
    return out


def unavailable(checks: list[Check], reason: str) -> list[CheckResult]:
    return [_result(c, "unavailable", reason, "aux.vlm") if c.applicable else
            _result(c, "not_applicable", "no project style guide text", "aux.vlm") for c in checks]


def _call(env: TaskEnv, images: list[tuple[bytes, str, str]], group: list[Check], context: str,
          tag: str) -> list[CheckResult]:
    aux = env.aux
    assert aux is not None
    try:
        with env.call(tag):
            res = aux.compare(images=images, questions=[(c.id, c.question) for c in group], context=context,
                              epoch=env.epoch("aux"), execution_id=derived_id("att", env.task.id, tag))
    except EngineRejected as e:
        return [_result(c, "unavailable", f"VLM rejected the request: {e}", "aux.vlm") for c in group]
    return _answers(res, group)


def _source_bytes(env: TaskEnv, job: Job) -> tuple[bytes | None, str | None]:
    plan = _plan_of(env.ctx, job)
    ref = next((r for r in plan.references if r.role == "primary"), None)
    if ref is None:
        return None, None
    return env.ctx.store.artifact_bytes(ref.artifact_id), ref.sha256


def _candidate_results(env: TaskEnv, checks: list[Check], cand: bytes, source: bytes | None, context: str,
                       cid: str, refs: dict[str, bytes]) -> list[CheckResult]:
    out: list[CheckResult] = []
    src = [c for c in checks if c.group == "source"]
    todo = [c for c in src if c.applicable]
    if todo:
        if source is None:
            out += [_result(c, "unavailable", "the plan has no primary source reference image", "aux.vlm")
                    for c in todo]
        else:
            out += _call(env, [(source, "source", ""), (cand, "candidate", "")], todo, context, f"{cid}|source")
    out += [_result(c, "not_applicable", "no project style guide text", "aux.vlm") for c in src if not c.applicable]
    for c in (x for x in checks if x.group != "source"):
        ref = refs.get(c.id)
        if ref is None:
            out.append(_result(c, "unavailable", "the reference image could not be read", "aux.vlm"))
        else:
            out += _call(env, [(ref, "reference", c.note), (cand, "candidate", "")], [c], context, f"{cid}|{c.id}")
    return out


def _load_references(env: TaskEnv, checks: list[Check]) -> dict[str, bytes]:
    refs: dict[str, bytes] = {}
    for c in checks:
        if c.reference_id:
            try:
                refs[c.id] = crop_png(env.ctx.store.artifact_bytes(c.reference_id), c.crop)
            except Exception:  # unreadable reference: that check becomes unavailable, others still run
                continue
    return refs


def qa_compare(env: TaskEnv) -> dict[str, Any]:
    t, store = env.task, env.ctx.store
    item, _ = load_item(store, t.job_id, t.item_id)
    if item.current_set != t.inputs["candidate_set_id"]:
        return {"skipped": "candidate set superseded"}
    cset = load_cset(store, t.job_id, item.current_set)
    job, _ = load_job(store, t.job_id)
    snap = store.read_snapshot(item.snapshot_sha)
    checks = compare_checks(job, item, snap)
    source, source_sha = _source_bytes(env, job) if is_generative_variant(job) else (None, None)
    refs = _load_references(env, checks)
    context = load_prompt(store, t.job_id, cset.prompt_revision_id).positive
    out: dict[str, Any] = {}
    for cand in cset.candidates:
        env.check_cancel()
        results = _candidate_results(env, checks, store.artifact_bytes(cand.artifact_id), source, context, cand.id,
                                     refs)
        out[cand.id] = [r.model_dump() for r in results]
    shas = {c.id: store.artifact(c.reference_id).sha256 for c in checks if c.reference_id and c.id in refs}
    return {"compare": out, "inputs": {"source_reference_sha": source_sha, "reference_shas": shas}}
