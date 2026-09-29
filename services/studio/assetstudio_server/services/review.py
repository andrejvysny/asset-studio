"""Review gates: exact candidate approval, deliberate bulk proposals, final acceptance."""
from __future__ import annotations

import hashlib
from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.domain import BatchItem, ReviewDecision
from assetstudio_core.ids import derived_id
from assetstudio_core.review import ApprovalRequest, ReviewError, check_binding, propose_best
from pydantic import BaseModel, Field

from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from .prompts import ItemRef, _outcome
from .records import decision_key, load_build, load_cset, load_qa


class ApproveItem(ItemRef):
    candidate_set_id: str
    candidate_id: str
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_revision_id: str
    qa_evaluation_id: str | None
    override_qa: bool = False
    override_reason: str | None = Field(default=None, max_length=1000)


class Approve(BaseModel):
    items: list[ApproveItem] = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=100)


class ClearApproval(BaseModel):
    items: list[ItemRef] = Field(min_length=1, max_length=200)


class PreviewBest(BaseModel):
    item_ids: list[str] | None = None


class AcceptItem(ItemRef):
    build_run_id: str
    accept: bool = True


class AcceptBuilds(BaseModel):
    items: list[AcceptItem] = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=100)


def _blob_sha(ctx: ProjectContext, artifact_id: str) -> str | None:
    """Re-hash the stored bytes: the approval binds content, not a filename or a cached claim."""
    art = ctx.store.artifact(artifact_id)
    with ctx.store.repo.open_blob(art.sha256) as f:
        h = hashlib.sha256()
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def approve(studio: Studio, ctx: ProjectContext, batch_id: str, req: Approve) -> dict[str, Any]:
    ctx.require_writable()
    if (prior := studio.journal.command_result(req.idempotency_key, req.model_dump(mode="json"))) is not None:
        return prior
    results = []
    for a in req.items:
        def apply(item: BatchItem, a: ApproveItem = a) -> None:
            cset = load_cset(ctx.store, batch_id, a.candidate_set_id) if item.current_set == a.candidate_set_id \
                else None
            qa = load_qa(ctx.store, batch_id, a.qa_evaluation_id) if a.qa_evaluation_id else None
            cand = next((c for c in cset.candidates if c.id == a.candidate_id), None) if cset else None
            payload = check_binding(item, cset, qa, ApprovalRequest(
                item_id=item.id, candidate_set_id=a.candidate_set_id, candidate_id=a.candidate_id,
                image_sha256=a.image_sha256, prompt_revision_id=a.prompt_revision_id,
                qa_evaluation_id=a.qa_evaluation_id, expected_item_revision=a.expected_item_revision,
                override_qa=a.override_qa, override_reason=a.override_reason),
                _blob_sha(ctx, cand.artifact_id) if cand else None)
            did = derived_id("dec", req.idempotency_key, item.id)
            if ctx.store.repo.stat_object(decision_key(batch_id, did)) is None:
                ctx.store.create(decision_key(batch_id, did), ReviewDecision(
                    id=did, gate="candidate_approval", batch_id=batch_id, item_id=item.id, decided_at=now_iso(),
                    idempotency_key=req.idempotency_key, **payload))
            item.decisions.append(did)
            item.approval = did
            item.regen_requested = False
        try:
            results.append(_outcome(studio, ctx, batch_id, a.item_id, apply, a.expected_item_revision))
        except ReviewError as e:
            results.append({"item_id": a.item_id, "ok": False, "code": e.code, "message": str(e), "detail": e.detail})
    response = {"results": results}
    studio.journal.record_command(req.idempotency_key, req.model_dump(mode="json"), response)
    return response


def clear_approval(studio: Studio, ctx: ProjectContext, batch_id: str, req: ClearApproval) -> list[dict[str, Any]]:
    out = []
    for r in req.items:
        def apply(item: BatchItem) -> None:
            if item.current_build is not None and item.tasks.get("build") and \
                    item.tasks["build"].state in ("queued", "running"):
                raise ApiError(409, "build_in_progress", "cancel the running build first")
            if item.accepted_build is not None:
                raise ApiError(409, "accepted", "the build result is accepted; unaccept it first")
            item.approval = None
            item.current_build = None
        out.append(_outcome(studio, ctx, batch_id, r.item_id, apply, r.expected_item_revision))
    return out


def preview_best(ctx: ProjectContext, batch_id: str, req: PreviewBest) -> dict[str, Any]:
    from .records import load_batch, load_items

    batch, _ = load_batch(ctx.store, batch_id)
    items = [i for i in load_items(ctx.store, batch) if req.item_ids is None or i.id in req.item_ids]
    proposals = []
    for item in items:
        cset = load_cset(ctx.store, batch_id, item.current_set) if item.current_set else None
        qas = {qid: load_qa(ctx.store, batch_id, qid) for qid in item.qa.values()}
        proposals.append({**propose_best(item, cset, qas), "name": item.name})
    return {"policy": "fewest failed minor checks, then fewest unavailable, then candidate order; recommended only",
            "proposals": [p for p in proposals if "skip" not in p],
            "skipped": [p for p in proposals if "skip" in p]}


def accept_builds(studio: Studio, ctx: ProjectContext, batch_id: str, req: AcceptBuilds) -> dict[str, Any]:
    """Final human acceptance of an actual built result. Structural validity is mandatory, never overridable."""
    ctx.require_writable()
    if (prior := studio.journal.command_result(req.idempotency_key, req.model_dump(mode="json"))) is not None:
        return prior
    results = []
    for a in req.items:
        def apply(item: BatchItem, a: AcceptItem = a) -> None:
            if item.current_build != a.build_run_id:
                raise ApiError(409, "stale_build", "the build result changed; reload")
            run, _ = load_build(ctx.store, batch_id, a.build_run_id)
            if not a.accept:
                if item.tasks.get("publish") and item.tasks["publish"].state in ("queued", "running"):
                    raise ApiError(409, "publishing", "publication is in progress")
                item.accepted_build = None
                return
            if run.status != "succeeded" or run.result != "valid":
                raise ApiError(409, "invalid_build", f"build is {run.status}/{run.result}: only valid results "
                                                     "can be accepted")
            did = derived_id("dec", req.idempotency_key, item.id)
            if ctx.store.repo.stat_object(decision_key(batch_id, did)) is None:
                ctx.store.create(decision_key(batch_id, did), ReviewDecision(
                    id=did, gate="final_acceptance", batch_id=batch_id, item_id=item.id,
                    bound={"build_run_id": run.id, "artifacts": run.artifacts}, decided_at=now_iso(),
                    idempotency_key=req.idempotency_key))
            item.decisions.append(did)
            item.accepted_build = run.id
        results.append(_outcome(studio, ctx, batch_id, a.item_id, apply, a.expected_item_revision))
    response = {"results": results}
    studio.journal.record_command(req.idempotency_key, req.model_dump(mode="json"), response)
    return response

