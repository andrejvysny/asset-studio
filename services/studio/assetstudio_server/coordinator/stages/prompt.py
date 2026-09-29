"""Enhancement stage (GPU1 aux VLM residency): one item per task; a pass drains eligible items of every Job."""
from __future__ import annotations

from typing import Any

from assetstudio_core.domain import JobItem
from assetstudio_core.ids import derived_id
from assetstudio_core.kinds import KINDS, Kind

from ...adapters.base import EngineRejected
from ...services.promptrev import make_revision
from ...services.records import load_item, mutate_item
from ..errors import Blocked, ItemFailed
from ..runner import TaskEnv


def enhance(env: TaskEnv) -> dict[str, Any]:
    aux = env.studio.aux
    if aux is None:
        raise Blocked("no aux service configured (library-only mode)", "aux_unconfigured", operator=True)
    t = env.task
    item, _ = load_item(env.ctx.store, t.job_id, t.item_id)
    if item.current_set is not None and not item.regen_requested:
        return {"skipped": "candidates exist; the prompt is locked"}
    if item.current_prompt != t.inputs.get("from_prompt"):
        return {"skipped": "the prompt was edited after enhancement was requested"}
    snap = env.ctx.store.read_snapshot(item.snapshot_sha)
    try:
        res = aux.enhance(brief=item.brief or item.name, kind=KINDS[Kind(snap["recipe"]["kind"])].label,
                          constraints=snap["template"], style_guide=(snap.get("style") or {}).get("guide", ""),
                          epoch=env.epoch("aux"), execution_id=derived_id("att", t.id, str(t.attempts)))
    except EngineRejected as e:
        raise ItemFailed(f"enhancer rejected the brief: {e}"[:300], "input_invalid") from e
    description = res.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ItemFailed("enhancer returned no description", "output_invalid")
    meta = res.get("meta") or {}
    enhancer = {"raw": meta.get("raw"), "model": meta.get("model"), "seconds": meta.get("seconds"),
                "short_title": res.get("short_title"), "tags": res.get("tags", []), "simulated": aux.simulated,
                "task_id": t.id, "residency": t.residency}
    rev_id = derived_id("prm", t.id)

    def apply(x: JobItem) -> None:
        rev = make_revision(env.ctx.store, x, rid=rev_id, origin="enhanced", description=description,
                            enhancer=enhancer)
        if rev.id not in x.prompt_revisions:
            x.prompt_revisions.append(rev.id)
        x.current_prompt = rev.id
        x.prompt_confirmed = None
    mutate_item(env.studio, env.ctx, t.job_id, t.item_id, apply)
    return {"prompt_revision_id": rev_id}
