"""Review gates: exact candidate approval, deliberate bulk proposals, final acceptance.

Units may come from several Jobs (a Batch wave); each unit keeps its own immutable decision record."""
from __future__ import annotations

from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.domain import JobItem, ReviewDecision
from assetstudio_core.ids import derived_id
from assetstudio_core.review import ApprovalRequest, ReviewError, check_binding, propose_best
from assetstudio_storage.repo import CorruptBlob
from pydantic import BaseModel, Field

from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from .production import refuse_direct
from .prompts import ItemRef, job_of, outcome
from .records import decision_key, load_build, load_cset, load_item, load_items, load_job, load_qa
from .runs import active_run_for, record_wave
from .taskview import busy, item_tasks


class ApproveItem(ItemRef):
    candidate_set_id: str
    candidate_id: str
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_revision_id: str
    qa_evaluation_id: str | None
    override_qa: bool = False
    override_reason: str | None = Field(default=None, max_length=1000)


class Approve(BaseModel):
    items: list[ApproveItem] = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=100)


class ClearApproval(BaseModel):
    items: list[ItemRef] = Field(min_length=1, max_length=500)


class PreviewBest(BaseModel):
    item_ids: list[str] | None = None


class AcceptItem(ItemRef):
    build_run_id: str
    accept: bool = True


class AcceptBuilds(BaseModel):
    items: list[AcceptItem] = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=100)


def _blob_sha(ctx: ProjectContext, artifact_id: str) -> str | None:
    """Re-hash the stored bytes (no cache): the approval binds content, not a filename or a cached claim."""
    art = ctx.store.artifact(artifact_id)
    try:
        ctx.store.repo.verify_blob(art.sha256, art.size, use_cache=False)
    except CorruptBlob:
        return None
    return art.sha256


def _idem(studio: Studio, ctx: ProjectContext, action: str, key: str, body: dict[str, Any]) -> dict[str, Any] | None:
    from ..journal import IdempotencyConflict

    try:
        return studio.journal.command_result(ctx.id, action, key, body)
    except IdempotencyConflict as e:
        raise ApiError(409, e.code, str(e)) from e


def approve(studio: Studio, ctx: ProjectContext, job_id: str | None, req: Approve,
            run_id: str | None = None) -> dict[str, Any]:
    ctx.require_writable()
    body = {"job_id": job_id, "run_id": run_id, **req.model_dump(mode="json")}
    if (prior := _idem(studio, ctx, "approve", req.idempotency_key, body)) is not None:
        return prior
    cid = derived_id("cmd", ctx.id, "approve", req.idempotency_key)
    results, units = [], []
    if job_id is not None:
        refuse_direct(ctx, job_id)
    for a in req.items:
        try:
            jid = job_of(a, job_id)
            refuse_direct(ctx, jid)
        except ApiError as e:
            results.append({"item_id": a.item_id, "ok": False, "code": e.code, "message": e.message})
            continue
        tasks = item_tasks(studio, ctx.id, load_item(ctx.store, jid, a.item_id)[0])

        def apply(item: JobItem, a: ApproveItem = a, jid: str = jid, tasks: Any = tasks) -> None:
            cset = load_cset(ctx.store, jid, a.candidate_set_id) if item.current_set == a.candidate_set_id else None
            qa = load_qa(ctx.store, jid, a.qa_evaluation_id) if a.qa_evaluation_id else None
            cand = next((c for c in cset.candidates if c.id == a.candidate_id), None) if cset else None
            payload = check_binding(item, cset, qa, ApprovalRequest(
                item_id=item.id, candidate_set_id=a.candidate_set_id, candidate_id=a.candidate_id,
                image_sha256=a.image_sha256, prompt_revision_id=a.prompt_revision_id,
                qa_evaluation_id=a.qa_evaluation_id, expected_item_revision=a.expected_item_revision,
                override_qa=a.override_qa, override_reason=a.override_reason),
                _blob_sha(ctx, cand.artifact_id) if cand else None, build_busy=busy(tasks, "build"))
            did = derived_id("dec", cid, item.id)
            if ctx.store.repo.stat_object(decision_key(jid, did)) is None:
                ctx.store.create(decision_key(jid, did), ReviewDecision(
                    id=did, gate="candidate_approval", job_id=jid, item_id=item.id, decided_at=now_iso(),
                    idempotency_key=req.idempotency_key, run_id=run_id or active_run_for(studio, ctx, jid),
                    wave_id=derived_id("wav", cid) if run_id else None, **payload))
            item.decisions.append(did)
            item.approval = did
            item.regen_requested = False
        try:
            r = outcome(studio, ctx, jid, a.item_id, apply, a.expected_item_revision)
        except ReviewError as e:
            r = {"job_id": jid, "item_id": a.item_id, "ok": False, "code": e.code, "message": str(e),
                 "detail": e.detail}
        results.append(r)
        if r["ok"]:
            units.append({"job_id": jid, "item_id": a.item_id, "approval_id": derived_id("dec", cid, a.item_id)})
    record_wave(studio, ctx, {"run_id": run_id, "wave_id": derived_id("wav", cid) if run_id else None,
                              "units": units}, cid, "candidate_approval")
    response = {"results": results}
    studio.journal.record_command(ctx.id, "approve", req.idempotency_key, body, response)
    return response


def clear_approval(studio: Studio, ctx: ProjectContext, job_id: str | None,
                   req: ClearApproval) -> list[dict[str, Any]]:
    out = []
    for r in req.items:
        jid = job_of(r, job_id)
        tasks = item_tasks(studio, ctx.id, load_item(ctx.store, jid, r.item_id)[0])

        def apply(item: JobItem, tasks: Any = tasks) -> None:
            if busy(tasks, "build"):
                raise ApiError(409, "build_in_progress", "cancel the running build first")
            if item.accepted_build is not None:
                raise ApiError(409, "accepted", "the build result is accepted; unaccept it first")
            item.approval = None
            item.current_build = None
        out.append(outcome(studio, ctx, jid, r.item_id, apply, r.expected_item_revision))
    return out


def preview_best(ctx: ProjectContext, job_ids: list[str], req: PreviewBest) -> dict[str, Any]:
    """A proposal only: nothing is approved until a human confirms exact candidates."""
    proposals = []
    for jid in job_ids:
        job, _ = load_job(ctx.store, jid)
        for item in load_items(ctx.store, job):
            if req.item_ids is not None and item.id not in req.item_ids:
                continue
            cset = load_cset(ctx.store, jid, item.current_set) if item.current_set else None
            qas = {qid: load_qa(ctx.store, jid, qid) for qid in item.qa.values()}
            proposals.append({**propose_best(item, cset, qas), "name": item.name, "job_id": jid})
    return {"policy": "fewest failed minor checks, then fewest unavailable, then candidate order; recommended only",
            "proposals": [p for p in proposals if "skip" not in p],
            "skipped": [p for p in proposals if "skip" in p]}


def accept_builds(studio: Studio, ctx: ProjectContext, job_id: str | None, req: AcceptBuilds,
                  run_id: str | None = None) -> dict[str, Any]:
    """Final human acceptance of an actual built result. Structural validity is mandatory, never overridable."""
    ctx.require_writable()
    body = {"job_id": job_id, "run_id": run_id, **req.model_dump(mode="json")}
    if (prior := _idem(studio, ctx, "accept_builds", req.idempotency_key, body)) is not None:
        return prior
    cid = derived_id("cmd", ctx.id, "accept_builds", req.idempotency_key)
    results, units = [], []
    for a in req.items:
        jid = job_of(a, job_id)
        tasks = item_tasks(studio, ctx.id, load_item(ctx.store, jid, a.item_id)[0])

        def apply(item: JobItem, a: AcceptItem = a, jid: str = jid, tasks: Any = tasks) -> None:
            if item.current_build != a.build_run_id:
                raise ApiError(409, "stale_build", "the build result changed; reload")
            run, _ = load_build(ctx.store, jid, a.build_run_id)
            if not a.accept:
                if busy(tasks, "publish"):
                    raise ApiError(409, "publishing", "publication is in progress")
                item.accepted_build = None
                return
            if run.status != "succeeded" or run.result != "valid":
                raise ApiError(409, "invalid_build", f"build is {run.status}/{run.result}: only valid results "
                                                     "can be accepted")
            did = derived_id("dec", cid, item.id)
            if ctx.store.repo.stat_object(decision_key(jid, did)) is None:
                ctx.store.create(decision_key(jid, did), ReviewDecision(
                    id=did, gate="final_acceptance", job_id=jid, item_id=item.id,
                    bound={"build_run_id": run.id, "artifacts": run.artifacts}, decided_at=now_iso(),
                    idempotency_key=req.idempotency_key, run_id=run_id or active_run_for(studio, ctx, jid)))
            item.decisions.append(did)
            item.accepted_build = run.id
        r = outcome(studio, ctx, jid, a.item_id, apply, a.expected_item_revision)
        results.append(r)
        if r["ok"] and a.accept:
            units.append({"job_id": jid, "item_id": a.item_id, "build_run_id": a.build_run_id})
    record_wave(studio, ctx, {"run_id": run_id, "wave_id": derived_id("wav", cid) if run_id else None,
                              "units": units}, cid, "final_acceptance")
    response = {"results": results}
    studio.journal.record_command(ctx.id, "accept_builds", req.idempotency_key, body, response)
    return response
