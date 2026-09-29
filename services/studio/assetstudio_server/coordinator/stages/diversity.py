"""Selection diversity stage (aux /compare over pairs of currently selected variants). Advisory report only: it
never approves, rejects or invalidates anything."""
from __future__ import annotations

from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.ids import derived_id

from ...adapters.base import EngineRejected
from ..runner import TaskEnv

RULESET = "diversity.v1"
QUESTION_ID = "diversity_distinct"
QUESTION = ("Are these two objects meaningfully different designs (not just a different camera angle, "
            "background or colour)?")


def report_key(report_id: str) -> str:
    return f"comparisons/variants/{report_id}.json"


def _pair_result(env: TaskEnv, sel: list[dict[str, Any]], i: int, j: int) -> dict[str, Any]:
    a, b = sel[i], sel[j]
    base = {"a": a["job_id"], "b": b["job_id"]}
    if a["sha256"] == b["sha256"]:
        return {**base, "result": "fail", "reason": "identical image (same sha256)", "evaluated": "deterministic"}
    aux, store = env.studio.aux, env.ctx.store
    assert aux is not None
    try:
        res = aux.compare(images=[(store.artifact_bytes(a["artifact_id"]), "candidate", "A"),
                                  (store.artifact_bytes(b["artifact_id"]), "candidate", "B")],
                          questions=[(QUESTION_ID, QUESTION)], context="", epoch=env.epoch("aux"),
                          execution_id=derived_id("att", env.task.id, str(i), str(j)))
    except EngineRejected as e:
        return {**base, "result": "unavailable", "reason": f"VLM rejected the request: {e}"[:300]}
    answer = (res.get("checks") or {}).get(QUESTION_ID)
    reason = str((res.get("reasons") or {}).get(QUESTION_ID) or "")[:300]
    model = str((res.get("meta") or {}).get("model", "vlm"))
    if isinstance(answer, bool):
        return {**base, "result": "pass" if answer else "fail", "reason": reason, "evaluator": model}
    return {**base, "result": "unavailable", "reason": reason or "VLM was unsure", "evaluator": model}


def run(env: TaskEnv) -> dict[str, Any]:
    t, ins = env.task, env.task.inputs
    store = env.ctx.store
    key = report_key(ins["report_id"])
    if store.repo.stat_object(key) is not None:  # a replay of a finished task
        return {"report_id": ins["report_id"], "pairs": len(ins["pairs"])}
    sel, pairs = ins["selection"], []
    for n, (i, j) in enumerate(ins["pairs"]):
        env.check_cancel()
        pairs.append(_pair_result(env, sel, i, j))
        env.progress(done=n + 1, total=len(ins["pairs"]))
    total = len(sel) * (len(sel) - 1) // 2
    aux = env.studio.aux
    store.create_or_same(key, {
        "id": ins["report_id"], "plan_id": ins["plan_id"], "selection": sel, "digest": ins["digest"],
        "ruleset": RULESET, "evaluator": next((p["evaluator"] for p in pairs if p.get("evaluator")), "aux.vlm"),
        "simulated": bool(aux and aux.simulated), "created_at": now_iso(), "task_id": t.id,
        "deterministic": ins["deterministic"], "pairs": pairs,
        "coverage": "full" if len(pairs) == total else "partial", "total_pairs": total,
        "not_evaluated_pairs": total - len(pairs)})
    return {"report_id": ins["report_id"], "pairs": len(pairs)}
