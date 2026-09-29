"""Candidate generation stage (GPU0 image engine). One item per task; a pass keeps one model residency across
every Job's confirmed items. Deterministic engine prompt ids make every submission reconcilable."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.domain import Candidate, CandidateSet, JobItem
from assetstudio_core.ids import derived_id
from assetstudio_core.seeds import derive_seed
from assetstudio_processing.images import ImageRejected, inspect_image

from ...adapters.base import EngineRejected, ImageEditRequest, ImageEngine, LoraUse, T2IRequest, engine_prompt_id
from ...models import load_lock
from ...services.records import cset_key, load_item, load_job, load_prompt, mutate_item
from ...services.variant_gen import SourceIntegrityError, VariantSource, primary_bytes, variant_source
from ..errors import Blocked, ItemFailed
from ..runner import TaskEnv

POLL_S = 1.0
TIMEOUT_S = 30 * 60
EDIT_MODELS = ["qwen_image_edit_2511", "qwen_image_2512"]
EDIT_DEFAULT_PARAMS = {"steps": 40, "cfg": 4.0}
EDIT_WORKFLOW_ID = "comfyui.qwen_edit_2511"  # the registered image_edit binding (comfyui/workflows)


def loras(env: TaskEnv, snap: dict[str, Any]) -> tuple[LoraUse | None, LoraUse | None, dict[str, Any]]:
    params = snap["parameters"]
    lock = load_lock(env.studio.settings.config_dir)
    speed = None
    preset = params.get("speed_preset", "quality")
    over: dict[str, Any] = {}
    if preset != "quality":
        spec = lock.get("loras", {}).get(preset)
        if spec is None:
            raise Blocked(f"speed preset {preset} has no LoRA in models.lock.yaml", "missing_lora", operator=True)
        speed = LoraUse(spec["file"], 1.0)
        over = {"steps": spec["steps"], "cfg": spec["cfg"]}
    style = None
    lora = (snap.get("values") or {}).get("style_lora")
    if lora:
        # Style-LoRA support is deferred: a snapshot that requires one is never run with it silently removed.
        reg = env.studio.extras.get("style_loras", {}).get(lora["model_id"])
        if reg is None:
            raise ItemFailed(f"this Job requires style LoRA {lora['model_id']} (style LoRAs are planned separately);"
                             " fork the Job without it to continue", "style_lora_unavailable")
        style = LoraUse(reg["file"], float(lora["strength"]))
    return style, speed, over


def _await(env: TaskEnv, prompt_id: str) -> None:
    engine = env.studio.engine
    assert engine is not None
    deadline = time.monotonic() + TIMEOUT_S
    while True:
        env.check_cancel_or(lambda: engine.cancel(prompt_id))
        st = engine.status(prompt_id)
        if st.state == "succeeded":
            return
        if st.state == "failed":
            raise EngineRejected(st.error or "generation failed")
        if st.state == "unknown":
            raise EngineRejected("engine lost the job")
        if time.monotonic() > deadline:
            engine.cancel(prompt_id)
            raise EngineRejected("generation timed out")
        time.sleep(POLL_S)


def _edit_params(snap: dict[str, Any]) -> dict[str, Any]:
    """Edit-path sampler settings; speed/style LoRAs do not exist for the edit model."""
    if (snap.get("values") or {}).get("style_lora"):
        raise ItemFailed("this Job requires a style LoRA, which the image-edit model does not support;"
                         " fork the Job without it to continue", "style_lora_unavailable")
    given = (snap.get("variant") or {}).get("generation_params") or {}
    return {k: type(v)(given.get(k, v)) for k, v in EDIT_DEFAULT_PARAMS.items()}


def _submit(engine: ImageEngine, req: T2IRequest | ImageEditRequest, rec: dict[str, Any]) -> dict[str, Any] | None:
    """Reconcile by the deterministic prompt id: only a prompt the engine does not know is (re)submitted."""
    if engine.status(rec["prompt_id"]).state != "unknown":
        return rec.get("receipt")
    if isinstance(req, ImageEditRequest):
        return engine.submit_edit(req)
    engine.submit(req)
    return None


def _slot(env: TaskEnv, engine: ImageEngine, req: T2IRequest | ImageEditRequest, rec: dict[str, Any],
          state: dict[str, Any], key: str) -> bytes:
    receipt = _submit(engine, req, rec)
    state[key] = {**rec, "submitted": True, **({"receipt": receipt} if receipt else {})}  # before awaiting
    env.progress(engine=state)
    _await(env, rec["prompt_id"])
    if isinstance(req, ImageEditRequest):
        # A crash between submit and persisting the receipt leaves no receipt: the output node must still come from
        # the edit workflow, never the adapter's default (T2I) one.
        return engine.fetch_image(rec["prompt_id"], (receipt or {}).get("workflow") or EDIT_WORKFLOW_ID)
    return engine.fetch_image(rec["prompt_id"])


def _edit_request(env: TaskEnv, prompt: Any, vs: VariantSource, image: bytes, rec: dict[str, Any], seed: int,
                  params: dict[str, Any]) -> ImageEditRequest:
    t = env.task
    return ImageEditRequest(
        prompt_id=rec["prompt_id"], source_sha256=vs.source_sha256, prepared_input_sha256=vs.primary.sha256,
        image=image, positive=prompt.positive, negative=prompt.negative, seed=seed, steps=params["steps"],
        cfg=params["cfg"], filename_prefix=f"assetstudio/{t.job_id}/{t.item_id}")


def _t2i_request(env: TaskEnv, prompt: Any, params: dict[str, Any], over: dict[str, Any], rec: dict[str, Any],
                 seed: int, style: LoraUse | None, speed: LoraUse | None) -> T2IRequest:
    t = env.task
    return T2IRequest(prompt_id=rec["prompt_id"], positive=prompt.positive, negative=prompt.negative, seed=seed,
                      width=int(params["width"]), height=int(params["height"]),
                      steps=int(over.get("steps", params["steps"])), cfg=float(over.get("cfg", params["cfg"])),
                      filename_prefix=f"assetstudio/{t.job_id}/{t.item_id}", style_lora=style, speed_lora=speed)


def _generation(engine: ImageEngine, t: Any, params: dict[str, Any], plan: _Plan) -> dict[str, Any]:
    base = {**engine.describe(), "simulated": engine.simulated, "residency": t.residency}
    if plan.vs is not None:
        return {**base, "mode": "image_edit", "models": EDIT_MODELS, "conditioning": plan.vs.conditioning(),
                "params": plan.edit_params, "style_lora": None, "speed_lora": None,
                "speed_preset_note": "not applicable to edit model"}
    style, speed = plan.style, plan.speed
    return {**base, "params": {k: params[k] for k in ("width", "height", "steps", "cfg", "speed_preset")
                               if k in params} | plan.over,
            "style_lora": style.__dict__ if style else None, "speed_lora": speed.__dict__ if speed else None,
            "models": ["qwen_image_2512"]}


def _finish(x: JobItem, cs_id: str) -> None:
    """A new round becomes current. Earlier rounds stay reviewable: their QA moves to history and an existing
    approval or build is kept (approval binds an exact candidate of ANY round)."""
    if cs_id not in x.candidate_sets:
        x.candidate_sets.append(cs_id)
    if x.current_set != cs_id:
        if x.current_set is not None and x.qa:
            x.qa_history = {**x.qa_history, x.current_set: dict(x.qa)}
        x.current_set, x.qa, x.regen_requested = cs_id, {}, False
        if x.approval is None:
            x.current_build = None


@dataclass
class _Plan:
    """Everything fixed for the whole set: the conditioning input (edit path) or the LoRAs (T2I path)."""
    vs: VariantSource | None = None
    image: bytes = b""
    style: LoraUse | None = None
    speed: LoraUse | None = None
    over: dict[str, Any] = field(default_factory=dict)
    edit_params: dict[str, Any] = field(default_factory=dict)


def _prepare(env: TaskEnv, engine: ImageEngine, job: Any, snap: dict[str, Any]) -> _Plan:
    vs = variant_source(env.ctx, job)
    if vs is None:
        style, speed, over = loras(env, snap)
        return _Plan(style=style, speed=speed, over=over)
    if not engine.supports("image_edit"):
        raise Blocked("image-edit engine unavailable", "editing_model_unavailable", operator=True)
    edit_params = _edit_params(snap)
    try:
        return _Plan(vs=vs, image=primary_bytes(env.ctx, vs), edit_params=edit_params)
    except SourceIntegrityError as e:
        raise ItemFailed(str(e), "source_integrity_failed") from e


def _make_artifact(env: TaskEnv, engine: ImageEngine, plan: _Plan, req: T2IRequest | ImageEditRequest,
                   rec: dict[str, Any], state: dict[str, Any], key: str, prompt_id: str) -> None:
    """Run one slot and register its candidate artifact; raises EngineRejected / ImageRejected for this slot."""
    t = env.task
    data = _slot(env, engine, req, rec, state, key)
    info = inspect_image(data, ("PNG",))
    source = {"engine": engine.describe(), "prompt_id": rec["prompt_id"], "prompt_revision_id": prompt_id,
              "simulated": engine.simulated}
    if plan.vs is not None:
        source |= {"receipt": state[key].get("receipt"), "conditioning": plan.vs.conditioning()}
    art = env.ctx.store.register_artifact(
        data, "candidate", info.mime, meta={**info.as_meta(), "seed": rec["seed"]}, retention="candidate",
        artifact_id=derived_id("art", t.id, key), source=source)
    state[key] = {**state[key], "artifact_id": art.id, "sha256": art.sha256, "width": info.width,
                  "height": info.height}
    env.progress(engine=state, done=sum(1 for r in state.values() if "artifact_id" in r))


def generate(env: TaskEnv) -> dict[str, Any]:
    engine = env.studio.engine
    if engine is None:
        raise Blocked("no image engine configured (library-only mode)", "engine_unconfigured", operator=True)
    t, store = env.task, env.ctx.store
    job, _ = load_job(store, t.job_id)
    item, _ = load_item(store, t.job_id, t.item_id)
    prompt_id = t.inputs["prompt_revision_id"]
    if item.prompt_confirmed != prompt_id:
        raise ItemFailed("the prompt changed after confirmation; confirm the current revision", "stale_input")
    prompt = load_prompt(store, t.job_id, prompt_id)
    snap = store.read_snapshot(item.snapshot_sha)
    params = snap["parameters"]
    plan = _prepare(env, engine, job, snap)
    count = int(params.get("candidate_count", 4))
    cs_id = derived_id("cs", t.id)
    set_number = (item.candidate_sets.index(cs_id) if cs_id in item.candidate_sets else len(item.candidate_sets)) + 1
    state: dict[str, Any] = dict(t.progress.get("engine", {}))
    env.progress(done=sum(1 for r in state.values() if "artifact_id" in r), total=count)
    candidates: list[Candidate] = []
    for idx in range(count):
        env.check_cancel()
        key = str(idx)
        # Seeds derive from the item, its candidate-set number and index: never from Batch position or order.
        seed = derive_seed(job.seed_family, item.id, str(set_number), key)
        rec = state.get(key) or {"prompt_id": engine_prompt_id(t.id, key), "seed": seed}
        req: T2IRequest | ImageEditRequest = (
            _edit_request(env, prompt, plan.vs, plan.image, rec, seed, plan.edit_params) if plan.vs is not None
            else _t2i_request(env, prompt, params, plan.over, rec, seed, plan.style, plan.speed))
        if "artifact_id" not in rec:
            try:
                _make_artifact(env, engine, plan, req, rec, state, key, prompt.id)
            except (EngineRejected, ImageRejected) as e:
                state[key] = {**rec, "error": str(e)[:300]}
                env.progress(engine=state)
                continue
        r = state[key]
        candidates.append(Candidate(id=derived_id("cnd", cs_id, key), index=idx, artifact_id=r["artifact_id"],
                                    sha256=r["sha256"], seed=r["seed"], width=r["width"], height=r["height"],
                                    engine={"prompt_id": r["prompt_id"]}))
    if not candidates:
        errors = "; ".join(sorted({v.get("error", "") for v in state.values()}))[:300]
        raise ItemFailed(errors or "no candidates produced", "output_invalid")
    cset = CandidateSet(id=cs_id, item_id=item.id, number=set_number, prompt_revision_id=prompt.id,
                        created_at=now_iso(), op_id=t.id, requested=count, candidates=candidates,
                        generation=_generation(engine, t, params, plan))
    if store.repo.stat_object(cset_key(t.job_id, cs_id)) is None:
        store.create(cset_key(t.job_id, cs_id), cset)
    mutate_item(env.studio, env.ctx, t.job_id, item.id, lambda x: _finish(x, cs_id))
    return {"candidate_set_id": cs_id, "candidates": len(candidates), "requested": count}
