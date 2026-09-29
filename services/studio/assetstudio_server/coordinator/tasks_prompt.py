"""Enhancement pass (GPU1 aux text model): one pass over the selected items, results durable per item."""
from __future__ import annotations

from typing import Any

from assetstudio_core.domain import BatchItem
from assetstudio_core.ids import derived_id
from assetstudio_core.kinds import KINDS, Kind

from ..adapters.base import EngineRejected, EngineUnavailable
from ..services.prompts import make_revision
from ..services.records import load_item, mutate_item, set_task
from .runner import Blocked, TaskEnv


def _items_error(env: TaskEnv, stage: str, state: str, message: str, ids: list[str]) -> None:
    for iid in ids:
        def apply(item: BatchItem) -> None:
            t = item.tasks.get(stage)
            if t is not None and t.op_id == env.op.id and t.state not in ("succeeded", "failed"):
                set_task(item, stage, env.op.id, state, message)
        mutate_item(env.studio, env.ctx, env.op.payload["batch_id"], iid, apply)


def enhance(env: TaskEnv) -> dict[str, Any]:
    aux = env.studio.aux
    if aux is None:
        raise Blocked("no aux service configured (library-only mode)", "aux_unconfigured", operator=True)
    env.studio.lanes["gpu1"].acquire("aux")
    batch_id = env.op.payload["batch_id"]
    done, failed = 0, 0
    for iid in env.op.payload["item_ids"]:
        env.check_cancel()
        item, _ = load_item(env.ctx.store, batch_id, iid)
        t = item.tasks.get("enhance")
        if t is None or t.op_id != env.op.id or t.state == "succeeded":
            continue  # superseded by a newer request, or already done before a restart
        mutate_item(env.studio, env.ctx, batch_id, iid, lambda x: set_task(x, "enhance", env.op.id, "running"))
        snap = env.ctx.store.read_snapshot(item.snapshot_sha)
        try:
            res = aux.enhance(brief=item.brief or item.name, kind=KINDS[Kind(snap["recipe"]["kind"])].label,
                              constraints=snap["template"], style_guide=(snap.get("style") or {}).get("guide", ""))
            description = res.get("description")
            if not isinstance(description, str) or not description.strip():
                raise EngineRejected("enhancer returned no description")
        except EngineUnavailable:
            raise
        except EngineRejected as e:
            failed += 1
            mutate_item(env.studio, env.ctx, batch_id, iid,
                        lambda x, e=e: set_task(x, "enhance", env.op.id, "failed", str(e)[:300]))
            continue
        rev_id = derived_id("prm", env.op.id, iid)

        meta = res.get("meta") or {}
        enhancer = {"raw": meta.get("raw"), "model": meta.get("model"), "seconds": meta.get("seconds"),
                    "short_title": res.get("short_title"), "tags": res.get("tags", []), "simulated": aux.simulated}

        def apply(x: BatchItem, rev_id: str = rev_id, enhancer: dict = enhancer, description: str = description,
                  ) -> None:
            rev = make_revision(env.ctx.store, x, rid=rev_id, origin="enhanced", description=description,
                                enhancer=enhancer)
            if rev.id not in x.prompt_revisions:
                x.prompt_revisions.append(rev.id)
            x.current_prompt = rev.id
            x.prompt_confirmed = None
            set_task(x, "enhance", env.op.id, "succeeded")
        mutate_item(env.studio, env.ctx, batch_id, iid, apply)
        done += 1
        env.progress(done=done, failed=failed, total=len(env.op.payload["item_ids"]))
    return {"enhanced": done, "failed": failed}


def enhance_error(env: TaskEnv, state: str, message: str) -> None:
    _items_error(env, "enhance", state, message, env.op.payload["item_ids"])
