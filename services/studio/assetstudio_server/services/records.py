"""Job record access helpers: reads, and the single optimistic item-mutation path."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.domain import (
    BuildRun,
    CandidateSet,
    Job,
    JobItem,
    PromptRevision,
    QaEvaluation,
    ReviewDecision,
    TaskRef,
)
from assetstudio_storage.project import ProjectStore, job_descriptor_key, job_key
from pydantic import BaseModel

from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio


def cmd_payload(req: BaseModel, job_id: str | None = None) -> dict[str, Any]:
    """What an idempotency key is bound to: the whole request plus its target Job (keys are project/action
    scoped by the journal)."""
    return {"job_id": job_id, **req.model_dump(mode="json")}


def item_key(batch_id: str, item_id: str) -> str:
    return job_key(batch_id, "items", f"{item_id}.json")


def prompt_key(batch_id: str, rid: str) -> str:
    return job_key(batch_id, "prompts", f"{rid}.json")


def cset_key(batch_id: str, cid: str) -> str:
    return job_key(batch_id, "candidates", f"{cid}.json")


def qa_key(batch_id: str, qid: str) -> str:
    return job_key(batch_id, "qa", f"{qid}.json")


def decision_key(batch_id: str, did: str) -> str:
    return job_key(batch_id, "decisions", f"{did}.json")


def build_key(batch_id: str, rid: str) -> str:
    return job_key(batch_id, "builds", f"{rid}.json")


def load_job(store: ProjectStore, batch_id: str) -> tuple[Job, str]:
    return store.get(job_descriptor_key(batch_id), Job)


def load_item(store: ProjectStore, batch_id: str, item_id: str) -> tuple[JobItem, str]:
    return store.get(item_key(batch_id, item_id), JobItem)


def load_items(store: ProjectStore, batch: Job) -> list[JobItem]:
    return [load_item(store, batch.id, i)[0] for i in batch.item_ids]


def load_prompt(store: ProjectStore, batch_id: str, rid: str) -> PromptRevision:
    return store.get(prompt_key(batch_id, rid), PromptRevision)[0]


def load_cset(store: ProjectStore, batch_id: str, cid: str) -> CandidateSet:
    return store.get(cset_key(batch_id, cid), CandidateSet)[0]


def load_qa(store: ProjectStore, batch_id: str, qid: str) -> QaEvaluation:
    return store.get(qa_key(batch_id, qid), QaEvaluation)[0]


def load_decision(store: ProjectStore, batch_id: str, did: str) -> ReviewDecision:
    return store.get(decision_key(batch_id, did), ReviewDecision)[0]


def load_build(store: ProjectStore, batch_id: str, rid: str) -> tuple[BuildRun, str]:
    return store.get(build_key(batch_id, rid), BuildRun)


def mutate_item(studio: Studio, ctx: ProjectContext, batch_id: str, item_id: str,
                fn: Callable[[JobItem], None], expected_revision: int | None = None) -> JobItem:
    """Read-check-apply-replace under the project lock + storage token. The only way item records change."""
    ctx.require_writable()
    with ctx.store.lock:
        item, token = load_item(ctx.store, batch_id, item_id)
        if expected_revision is not None and item.revision != expected_revision:
            raise ApiError(409, "stale_item", f"{item.name} changed (revision {item.revision}); reload",
                           {"item_id": item_id, "revision": item.revision})
        fn(item)
        item.revision += 1
        item.updated_at = now_iso()
        ctx.store.replace(item_key(batch_id, item_id), item, token)
    studio.events.publish("item", project_id=ctx.id, job_id=batch_id, batch_id=batch_id, item_id=item_id)
    return item


def set_task(item: JobItem, stage: str, op_id: str, state: str, error: str | None = None,
             progress: dict | None = None) -> None:
    item.tasks[stage] = TaskRef(op_id=op_id, state=state, error=error, progress=progress or {})  # type: ignore[arg-type]
