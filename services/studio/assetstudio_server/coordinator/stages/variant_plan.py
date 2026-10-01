"""Variant draft planning stages (GPU1 aux VLM residency): explicit source analysis and row suggestion.

Tasks are scoped to the draft (job_id = item_id = the draft id); results are written next to the draft as
records, never over the user's rows."""
from __future__ import annotations

from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.ids import derived_id
from assetstudio_core.variants import ReferenceImage, VariantDraft

from ...adapters.base import EngineRejected
from ...errors import ApiError
from ...services import variant_analysis as va
from ...services.variant_refs import load_reference_set
from ...services.variants import load_draft
from ..errors import Blocked, ItemFailed
from ..runner import TaskEnv
from .base import model_identity


def _draft_and_refs(env: TaskEnv) -> tuple[VariantDraft, dict[str, Any], list[ReferenceImage]]:
    try:
        draft, _ = load_draft(env.ctx, env.task.item_id)
        record, images = load_reference_set(env.ctx, draft)
    except ApiError as e:
        code = "references_missing" if e.status == 409 and e.code.startswith("reference") else e.code
        raise ItemFailed(e.message[:300], code) from e
    return draft, record, images


def _aux(env: TaskEnv) -> Any:
    aux = env.aux
    if aux is None:
        raise Blocked("no aux service configured (library-only mode)", "aux_unconfigured", operator=True)
    return aux


def _load_bytes(env: TaskEnv, images: list[ReferenceImage]) -> list[tuple[bytes, str]]:
    return [(env.ctx.store.artifact_bytes(i.artifact_id), i.view) for i in images]


def analyze(env: TaskEnv) -> dict[str, Any]:
    aux = _aux(env)
    t = env.task
    draft, record, images = _draft_and_refs(env)
    images = images[: va.MAX_ANALYSIS_IMAGES]
    model = model_identity(env.studio, va.VLM_MODEL)
    aid = va.analysis_id_for(record["id"], model, draft.request)
    rec = va.read_analysis(env.ctx, aid)
    cached = rec is not None
    if rec is None:
        env.check_cancel()
        try:
            with env.call("analyze"):
                res = aux.analyze_source(images=_load_bytes(env, images), kind=draft.source.kind.value,
                                         user_facts=draft.request, epoch=env.epoch("aux"),
                                         execution_id=derived_id("att", t.id, str(t.attempts)))
        except EngineRejected as e:
            raise ItemFailed(f"analysis rejected: {e}"[:300], "input_invalid") from e
        rec = {"id": aid, "draft_id": draft.id, "reference_set_id": record["id"],
               "input_hashes": [{"view": i.view, "sha256": i.sha256} for i in images], "model": model,
               "observations": res.get("observations") or [], "uncertainties": res.get("uncertainties") or [],
               "proposed_preserve": res.get("proposed_preserve") or [],
               "proposed_changeable": res.get("proposed_changeable") or [], "raw_ref": res.get("meta") or {},
               "created_at": now_iso()}
        env.ctx.store.create_or_same(va.analysis_key(aid), rec)
    va.attach_analysis(env.ctx, draft.id, aid)
    return {"analysis_id": aid, "cached": cached}


def suggest(env: TaskEnv) -> dict[str, Any]:
    aux = _aux(env)
    t = env.task
    draft, _, images = _draft_and_refs(env)
    ordered = sorted(images, key=lambda i: i.role != "primary")[: va.MAX_ANALYSIS_IMAGES]
    observations: list[str] | None = None
    if draft.analysis_id and (an := va.read_analysis(env.ctx, draft.analysis_id)) is not None:
        observations = [str(o.get("text", "")) for o in an["observations"] if isinstance(o, dict)]
    count, intent = int(t.inputs["count"]), str(t.inputs.get("intent") or "related")
    request = str(t.inputs.get("request") or "")
    env.check_cancel()
    try:
        with env.call("suggest"):
            res = aux.suggest_variants(
                images=_load_bytes(env, ordered), request=request, count=count, intent=intent,
                preserve="; ".join(str(x) for x in t.inputs.get("preserve") or []), kind=draft.source.kind.value,
                observations=observations, epoch=env.epoch("aux"),
                execution_id=derived_id("att", t.id, str(t.attempts)))
    except EngineRejected as e:
        raise ItemFailed(f"suggestion rejected: {e}"[:300], "input_invalid") from e
    rows = va.normalise_rows(res.get("rows") or [], count)
    if not rows:
        raise ItemFailed("the model returned no usable rows", "output_invalid")
    meta = res.get("meta") or {}
    va.attach_suggestion(env.ctx, draft.id, {
        "task_id": t.id, "rows": rows, "short_by": max(int(res.get("short_by") or 0), count - len(rows)),
        "notes": [str(n)[:300] for n in res.get("notes") or []], "model": meta.get("model"),
        "created_at": now_iso(), "count": count, "intent": intent, "request": request})
    return {"rows": len(rows)}
