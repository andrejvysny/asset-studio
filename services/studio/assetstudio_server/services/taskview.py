"""Item-level view of journal tasks: one entry per family (enhance/generate/qa/build/publish) for the UI."""
from __future__ import annotations

from typing import Any

from assetstudio_core.domain import JobItem, TaskRef

from ..studio import Studio
from ..taskstore import TASK_ACTIVE, StageTask

ACTIVE_VIEW = ("queued", "running", "reconciling")


def _aggregate(chain: list[StageTask]) -> TaskRef:
    lead = next((t for t in chain if t.state in TASK_ACTIVE), None) or next(
        (t for t in chain if t.state in ("failed", "cancelled")), None) or chain[-1]
    states = [t.state for t in chain]
    if any(s == "running" for s in states):
        state = "running"
    elif any(s in ("queued", "reconciling") for s in states):
        state = "queued"
    elif any(s == "blocked" for s in states):
        state = "blocked"
    elif any(s == "failed" for s in states):
        state = "failed"
    elif any(s == "cancelled" for s in states):
        state = "cancelled"
    else:
        state = "succeeded"
    err = lead.error or {}
    done = sum(1 for s in states if s == "succeeded")
    progress = {**lead.progress, "stage": lead.stage, "stages_done": done, "stages_total": len(chain)}
    progress.pop("engine", None)
    if state == "succeeded" and any(t.downstream_pending for t in chain):
        progress["downstream_pending"] = True  # QA (or other follow-up) is deferred behind an older owner
    return TaskRef(op_id=lead.id, state=state, error=err.get("message") if state != "succeeded" else None,  # type: ignore[arg-type]
                   progress=progress)


def item_tasks(studio: Studio, project_id: str, item: JobItem) -> dict[str, TaskRef]:
    """Latest chain per family (tasks of one command), aggregated; legacy (pre-journal) task refs as fallback."""
    chains: dict[str, dict[str, list[StageTask]]] = {}
    for t in studio.journal.tasks.list(project_id=project_id, item_id=item.id):
        chains.setdefault(t.family, {}).setdefault(t.command_id, []).append(t)
    out: dict[str, TaskRef] = {k: v for k, v in item.tasks.items() if v.state not in ACTIVE_VIEW}
    for family, by_cmd in chains.items():
        latest = max(by_cmd.values(), key=lambda c: max(t.seq for t in c))
        out[family] = _aggregate(sorted(latest, key=lambda t: t.seq))
    return out


def busy(tasks: dict[str, TaskRef], *families: str) -> bool:
    return any(tasks.get(f) is not None and tasks[f].state in ACTIVE_VIEW for f in families)


def as_json(tasks: dict[str, TaskRef]) -> dict[str, Any]:
    return {k: v.model_dump() for k, v in tasks.items()}
