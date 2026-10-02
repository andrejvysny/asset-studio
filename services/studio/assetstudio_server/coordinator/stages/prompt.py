"""Enhancement stage (GPU1 aux VLM residency): one item per task; a pass drains eligible items of every Job."""
from __future__ import annotations

from typing import Any

from assetstudio_core.domain import Job, JobItem, PromptRevision
from assetstudio_core.ids import derived_id
from assetstudio_core.kinds import KINDS, Kind
from assetstudio_storage.repo import IntegrityError, NotFound

from ...adapters.base import EngineRejected
from ...services.edit_templates import edit_template
from ...services.promptrev import make_revision
from ...services.promptvariants import variant_count, variation_brief
from ...services.records import load_item, load_job, mutate_item, prompt_key
from ...services.reference_bindings import ENHANCER_MAX_IMAGES, Selection, reference_images, resolve_references
from ...services.variant_gen import SourceIntegrityError, VariantSource, primary_bytes, variant_source
from ..errors import Blocked, ItemFailed
from ..runner import TaskEnv


def _locked(item: JobItem, rounds: bool) -> bool:
    if rounds:  # every round is kept: only an accepted build freezes the prompt
        return item.accepted_build is not None
    return item.current_set is not None and not item.regen_requested


def _call_args(env: TaskEnv, job: Job, item: JobItem, snap: dict[str, Any], vs: VariantSource | None
               ) -> tuple[dict[str, Any], Selection]:
    """Enhancer inputs: variant Jobs edit the plan's primary reference; others describe references for T2I.
    The source image counts against the aux image cap, so references get what is left."""
    try:
        images: list[tuple[bytes, str, str]] = []
        if vs is not None:
            images.append((primary_bytes(env.ctx, vs), "source", ""))
        sel = resolve_references(env.ctx.store, item, snap, "prompt_guidance",
                                 ENHANCER_MAX_IMAGES - (1 if vs is not None else 0))
        images += reference_images(env.ctx.store, sel)
    except (SourceIntegrityError, IntegrityError, NotFound) as e:
        raise ItemFailed(f"reference image failed verification: {e}"[:300], "source_integrity_failed") from e
    kind = snap["recipe"]["kind"]
    args: dict[str, Any] = {"preset": item.enhance_preset, "images": images}
    if vs is None:
        return {**args, "mode": "t2i", "brief": item.brief or item.name, "constraints": snap["template"]}, sel
    change = (job.variant or {}).get("change_request") or item.brief or item.name
    return {**args, "mode": "edit", "brief": change, "change": change, "preserve": vs.preserve_text(),
            "constraints": edit_template(kind)}, sel


def _bindings(item: JobItem, vs: VariantSource | None, mode: str, res: dict[str, Any], sel: Selection
              ) -> dict[str, Any]:
    return {"preset": item.enhance_preset, "mode": mode, "references_revision": item.references_revision,
            "reference_ids": sel.ids(), "references_excluded": sel.excluded_list(), **(vs.bindings() if vs else {}),
            **{k: res.get(k) or [] for k in ("facts", "additions", "assumptions", "reference_cues")}}


def _enhance_variant(env: TaskEnv, aux: Any, snap: dict[str, Any], args: dict[str, Any], index: int) -> dict[str, Any]:
    """One enhancer call. Variant 0 keeps the legacy call key / execution id (replay-compatible)."""
    t = env.task
    call_args = {**args, "brief": variation_brief(args["brief"], index)} if args["mode"] == "t2i" else args
    parts = (t.id, str(t.attempts)) if index == 0 else (t.id, str(t.attempts), f"v{index}")
    try:
        with env.call("enhance" if index == 0 else f"enhance.v{index}"):
            res = aux.enhance(kind=KINDS[Kind(snap["recipe"]["kind"])].label,
                              style_guide=(snap.get("style") or {}).get("guide", ""), epoch=env.epoch("aux"),
                              execution_id=derived_id("att", *parts), **call_args)
    except EngineRejected as e:
        raise ItemFailed(f"enhancer rejected the brief (prompt {index + 1}): {e}"[:300], "input_invalid") from e
    description = res.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ItemFailed(f"enhancer returned no description (prompt {index + 1})", "output_invalid")
    return res


def enhance(env: TaskEnv) -> dict[str, Any]:
    """Writes one prompt revision per candidate slot (see promptvariants). Each revision is stored as soon as it
    exists, so a retry after a failed variant calls the enhancer only for the missing ones."""
    aux = env.aux
    if aux is None:
        raise Blocked("no aux service configured (library-only mode)", "aux_unconfigured", operator=True)
    t, store = env.task, env.ctx.store
    job, _ = load_job(store, t.job_id)
    item, _ = load_item(store, t.job_id, t.item_id)
    if _locked(item, bool(t.inputs.get("rounds", True))):
        return {"skipped": "candidates exist; the prompt is locked"}
    if item.current_prompt != t.inputs.get("from_prompt"):
        return {"skipped": "the prompt was edited after enhancement was requested"}
    snap = store.read_snapshot(item.snapshot_sha)
    vs = variant_source(env.ctx, job)
    args, sel = _call_args(env, job, item, snap, vs)
    n = variant_count(snap, vs)
    numbering = item.model_copy(deep=True)  # revision numbers count the siblings written so far
    ids: list[str] = []
    for i in range(n):
        rid = derived_id("prm", t.id) if i == 0 else derived_id("prm", t.id, f"v{i}")
        if store.get_opt(prompt_key(t.job_id, rid), PromptRevision)[0] is None:
            res = _enhance_variant(env, aux, snap, args, i)
            meta = res.get("meta") or {}
            enhancer = {"raw": meta.get("raw"), "model": meta.get("model"), "seconds": meta.get("seconds"),
                        "short_title": res.get("short_title"), "tags": res.get("tags", []), "simulated": aux.simulated,
                        "task_id": t.id, "residency": t.residency}
            bindings = _bindings(item, vs, args["mode"], res, sel) | ({"variant_index": i, "variant_count": n}
                                                                      if n > 1 else {})
            make_revision(store, numbering, rid=rid, origin="enhanced", description=res["description"],
                          enhancer=enhancer, bindings=bindings)
        numbering.prompt_revisions.append(rid)
        ids.append(rid)

    def apply(x: JobItem) -> None:
        x.prompt_revisions += [r for r in ids if r not in x.prompt_revisions]
        x.current_prompt = ids[0]
        x.prompt_variants = ids if n > 1 else []
        x.prompt_confirmed = None
    mutate_item(env.studio, env.ctx, t.job_id, t.item_id, apply)
    return {"prompt_revision_id": ids[0], "variants": n}
