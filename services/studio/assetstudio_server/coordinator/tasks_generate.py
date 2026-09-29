"""Candidate generation pass (GPU0 ComfyUI). Deterministic engine prompt ids make every step reconcilable."""
from __future__ import annotations

import time
from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.domain import Candidate, CandidateSet, Job, JobItem
from assetstudio_core.ids import derived_id
from assetstudio_core.seeds import derive_seed
from assetstudio_processing.images import ImageRejected, inspect_image

from ..adapters.base import EngineRejected, LoraUse, T2IRequest, engine_prompt_id
from ..models import load_lock
from ..services.records import cset_key, load_item, load_job, load_prompt, mutate_item, set_task
from .runner import Blocked, TaskEnv
from .tasks_prompt import _items_error

POLL_S = 1.0
TIMEOUT_S = 30 * 60


def _loras(env: TaskEnv, snap: dict[str, Any]) -> tuple[LoraUse | None, LoraUse | None, dict[str, Any]]:
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
    lora = snap["values"].get("style_lora")
    if lora:
        reg = env.studio.extras.get("style_loras", {}).get(lora["model_id"])
        if reg is None:
            raise Blocked(f"style LoRA {lora['model_id']} is not registered on this host", "missing_lora",
                          operator=True)
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


def _one_item(env: TaskEnv, batch: Job, entry: dict[str, str]) -> str:
    store, engine = env.ctx.store, env.studio.engine
    assert engine is not None
    item, _ = load_item(store, batch.id, entry["item_id"])
    t = item.tasks.get("generate")
    if t is None or t.op_id != env.op.id:
        return "superseded"
    if t.state == "succeeded":
        return "done"
    if item.prompt_confirmed != entry["prompt_revision_id"]:
        mutate_item(env.studio, env.ctx, batch.id, item.id,
                    lambda x: set_task(x, "generate", env.op.id, "failed", "prompt changed after confirmation"))
        return "stale"
    prompt = load_prompt(store, batch.id, entry["prompt_revision_id"])
    snap = store.read_snapshot(item.snapshot_sha)
    params = snap["parameters"]
    style, speed, over = _loras(env, snap)
    count = int(params.get("candidate_count", 4))
    cs_id = derived_id("cs", env.op.id, item.id)
    set_number = (item.candidate_sets.index(cs_id) if cs_id in item.candidate_sets else len(item.candidate_sets)) + 1
    state: dict[str, Any] = env.op.engine.get(item.id, {})
    mutate_item(env.studio, env.ctx, batch.id, item.id,
                lambda x: set_task(x, "generate", env.op.id, "running", progress={"done": 0, "total": count}))
    candidates: list[Candidate] = []
    for idx in range(count):
        key = str(idx)
        seed = derive_seed(batch.seed_family, item.id, str(set_number), key)
        rec = state.get(key) or {"prompt_id": engine_prompt_id(env.op.id, item.id, key), "seed": seed}
        req = T2IRequest(prompt_id=rec["prompt_id"], positive=prompt.positive, negative=prompt.negative, seed=seed,
                         width=int(params["width"]), height=int(params["height"]),
                         steps=int(over.get("steps", params["steps"])), cfg=float(over.get("cfg", params["cfg"])),
                         filename_prefix=f"assetstudio/{batch.id}/{item.id}", style_lora=style, speed_lora=speed)
        if "artifact_id" not in rec:
            try:
                if engine.status(rec["prompt_id"]).state == "unknown":
                    engine.submit(req)  # never submitted, or the engine lost it: safe to (re)submit this exact id
                state[key] = {**rec, "submitted": True}
                env.engine_state(**{item.id: state})
                _await(env, rec["prompt_id"])
                data = engine.fetch_image(rec["prompt_id"])
                info = inspect_image(data, ("PNG",))
            except (EngineRejected, ImageRejected) as e:
                state[key] = {**rec, "error": str(e)[:300]}
                env.engine_state(**{item.id: state})
                continue
            art = store.register_artifact(data, "candidate", info.mime, meta={**info.as_meta(), "seed": seed},
                                          retention="candidate", source={
                                              "engine": engine.describe(), "prompt_id": rec["prompt_id"],
                                              "prompt_revision_id": prompt.id, "simulated": engine.simulated})
            state[key] = {**rec, "artifact_id": art.id, "sha256": art.sha256, "width": info.width,
                          "height": info.height}
            env.engine_state(**{item.id: state})
            mutate_item(env.studio, env.ctx, batch.id, item.id, lambda x, n=idx + 1: set_task(
                x, "generate", env.op.id, "running", progress={"done": n, "total": count}))
        r = state[key]
        candidates.append(Candidate(id=derived_id("cnd", cs_id, key), index=idx, artifact_id=r["artifact_id"],
                                    sha256=r["sha256"], seed=r["seed"], width=r["width"], height=r["height"],
                                    engine={"prompt_id": r["prompt_id"]}))
    if not candidates:
        errors = "; ".join(sorted({v.get("error", "") for v in state.values()}))[:300]
        mutate_item(env.studio, env.ctx, batch.id, item.id,
                    lambda x: set_task(x, "generate", env.op.id, "failed", errors or "no candidates produced"))
        return "failed"
    cset = CandidateSet(id=cs_id, item_id=item.id, number=set_number, prompt_revision_id=prompt.id,
                        created_at=now_iso(), op_id=env.op.id, requested=count, candidates=candidates, generation={
                            **engine.describe(), "simulated": engine.simulated, "params": {
                                k: params[k] for k in ("width", "height", "steps", "cfg", "speed_preset")
                                if k in params} | over,
                            "style_lora": style.__dict__ if style else None,
                            "speed_lora": speed.__dict__ if speed else None, "models": ["qwen_image_2512"]})
    if store.repo.stat_object(cset_key(batch.id, cs_id)) is None:
        store.create(cset_key(batch.id, cs_id), cset)

    def apply(x: JobItem) -> None:
        if cs_id not in x.candidate_sets:
            x.candidate_sets.append(cs_id)
        x.current_set, x.qa, x.approval, x.regen_requested = cs_id, {}, None, False
        x.current_build = None
        set_task(x, "generate", env.op.id, "succeeded", progress={"done": len(candidates), "total": count})
    mutate_item(env.studio, env.ctx, batch.id, item.id, apply)
    qa_op, created = env.studio.journal.enqueue(
        project_id=env.ctx.id, batch_id=batch.id, kind="qa", lane="gpu1", affinity="aux.qa",
        payload={"batch_id": batch.id, "item_id": item.id, "candidate_set_id": cs_id},
        idempotency_key=f"qa:{cs_id}", hold=True)
    if created:
        mutate_item(env.studio, env.ctx, batch.id, item.id, lambda x: set_task(x, "qa", qa_op.id, "queued"))
        env.studio.journal.release(qa_op.id)
    return "done"


def generate(env: TaskEnv) -> dict[str, Any]:
    if env.studio.engine is None:
        raise Blocked("no image engine configured (library-only mode)", "engine_unconfigured", operator=True)
    batch, _ = load_job(env.ctx.store, env.op.payload["batch_id"])
    outcomes: dict[str, str] = {}
    for n, entry in enumerate(env.op.payload["items"]):
        env.check_cancel()
        outcomes[entry["item_id"]] = _one_item(env, batch, entry)
        env.progress(items_done=n + 1, items_total=len(env.op.payload["items"]), engine_passes=1)
    return {"items": outcomes}


def generate_error(env: TaskEnv, state: str, message: str) -> None:
    _items_error(env, "generate", state, message, [e["item_id"] for e in env.op.payload["items"]])
