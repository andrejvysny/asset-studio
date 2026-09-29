"""Candidate generation stage (GPU0 image engine). One item per task; a pass keeps one model residency across
every Job's confirmed items. Deterministic engine prompt ids make every submission reconcilable."""
from __future__ import annotations

import time
from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.domain import Candidate, CandidateSet, JobItem
from assetstudio_core.ids import derived_id
from assetstudio_core.seeds import derive_seed
from assetstudio_processing.images import ImageRejected, inspect_image

from ...adapters.base import EngineRejected, LoraUse, T2IRequest, engine_prompt_id
from ...models import load_lock
from ...services.records import cset_key, load_item, load_job, load_prompt, mutate_item
from ..errors import Blocked, ItemFailed
from ..runner import TaskEnv

POLL_S = 1.0
TIMEOUT_S = 30 * 60


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
    style, speed, over = loras(env, snap)
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
        req = T2IRequest(prompt_id=rec["prompt_id"], positive=prompt.positive, negative=prompt.negative, seed=seed,
                         width=int(params["width"]), height=int(params["height"]),
                         steps=int(over.get("steps", params["steps"])), cfg=float(over.get("cfg", params["cfg"])),
                         filename_prefix=f"assetstudio/{t.job_id}/{item.id}", style_lora=style, speed_lora=speed)
        if "artifact_id" not in rec:
            try:
                if engine.status(rec["prompt_id"]).state == "unknown":
                    engine.submit(req)  # never submitted, or the engine lost it: safe to (re)submit this exact id
                state[key] = {**rec, "submitted": True}
                env.progress(engine=state)
                _await(env, rec["prompt_id"])
                data = engine.fetch_image(rec["prompt_id"])
                info = inspect_image(data, ("PNG",))
            except (EngineRejected, ImageRejected) as e:
                state[key] = {**rec, "error": str(e)[:300]}
                env.progress(engine=state)
                continue
            art = store.register_artifact(data, "candidate", info.mime, meta={**info.as_meta(), "seed": seed},
                                          retention="candidate", artifact_id=derived_id("art", t.id, key), source={
                                              "engine": engine.describe(), "prompt_id": rec["prompt_id"],
                                              "prompt_revision_id": prompt.id, "simulated": engine.simulated})
            state[key] = {**rec, "artifact_id": art.id, "sha256": art.sha256, "width": info.width,
                          "height": info.height}
            env.progress(engine=state, done=sum(1 for r in state.values() if "artifact_id" in r))
        r = state[key]
        candidates.append(Candidate(id=derived_id("cnd", cs_id, key), index=idx, artifact_id=r["artifact_id"],
                                    sha256=r["sha256"], seed=r["seed"], width=r["width"], height=r["height"],
                                    engine={"prompt_id": r["prompt_id"]}))
    if not candidates:
        errors = "; ".join(sorted({v.get("error", "") for v in state.values()}))[:300]
        raise ItemFailed(errors or "no candidates produced", "output_invalid")
    cset = CandidateSet(id=cs_id, item_id=item.id, number=set_number, prompt_revision_id=prompt.id,
                        created_at=now_iso(), op_id=t.id, requested=count, candidates=candidates, generation={
                            **engine.describe(), "simulated": engine.simulated, "params": {
                                k: params[k] for k in ("width", "height", "steps", "cfg", "speed_preset")
                                if k in params} | over,
                            "style_lora": style.__dict__ if style else None,
                            "speed_lora": speed.__dict__ if speed else None, "models": ["qwen_image_2512"],
                            "residency": t.residency})
    if store.repo.stat_object(cset_key(t.job_id, cs_id)) is None:
        store.create(cset_key(t.job_id, cs_id), cset)

    def apply(x: JobItem) -> None:
        if cs_id not in x.candidate_sets:
            x.candidate_sets.append(cs_id)
        if x.current_set != cs_id:
            x.current_set, x.qa, x.approval, x.regen_requested = cs_id, {}, None, False
            x.current_build = None
    mutate_item(env.studio, env.ctx, t.job_id, item.id, apply)
    return {"candidate_set_id": cs_id, "candidates": len(candidates), "requested": count}
