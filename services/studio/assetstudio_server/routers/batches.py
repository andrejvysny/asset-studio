"""Batch commands. Every mutation names exact revisions; long work returns durable operations (202)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..errors import ApiError
from ..journal import IdempotencyConflict
from ..registry import ProjectContext
from ..services import batches as bsvc
from ..services import production, prompts, review
from ..studio import Studio
from .deps import project, studio

router = APIRouter(prefix="/api/v1/projects/{project_id}/batches")


def _accepted(body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(body, status_code=202)


def _idem(fn: Any) -> Any:
    try:
        return fn()
    except IdempotencyConflict as e:
        raise ApiError(409, e.code, str(e)) from e


@router.get("")
def list_batches(ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    return {"batches": bsvc.list_batches(ctx)}


@router.post("", status_code=201)
def create(req: bsvc.CreateBatch, ctx: ProjectContext = Depends(project), s: Studio = Depends(studio)) -> Any:
    batch, created = _idem(lambda: bsvc.create_batch(s, ctx, req))
    body: dict[str, Any] = {"batch": bsvc.batch_summary(ctx, batch), "created": created, "enhance": None}
    if created and req.enhance:
        body["enhance"] = prompts.enqueue_enhance(s, ctx, batch.id, prompts.EnhanceRequest(
            item_ids=batch.item_ids, idempotency_key=f"{req.idempotency_key}:enhance"))
    return JSONResponse(body, status_code=201 if created else 200)


@router.get("/{batch_id}")
def detail(batch_id: str, ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    return bsvc.batch_detail(ctx, batch_id)


@router.post("/{batch_id}:enhance")
def enhance(batch_id: str, req: prompts.EnhanceRequest, ctx: ProjectContext = Depends(project),
            s: Studio = Depends(studio)) -> JSONResponse:
    return _accepted(_idem(lambda: prompts.enqueue_enhance(s, ctx, batch_id, req)))


@router.post("/{batch_id}:edit-prompts")
def edit_prompts(batch_id: str, req: prompts.EditPrompts, ctx: ProjectContext = Depends(project),
                 s: Studio = Depends(studio)) -> dict[str, Any]:
    return {"results": prompts.edit_prompts(s, ctx, batch_id, req)}


@router.post("/{batch_id}:confirm-and-generate")
def confirm(batch_id: str, req: prompts.ConfirmAndGenerate, ctx: ProjectContext = Depends(project),
            s: Studio = Depends(studio)) -> JSONResponse:
    return _accepted(_idem(lambda: prompts.confirm_and_generate(s, ctx, batch_id, req)))


@router.post("/{batch_id}:mark-regenerate")
def mark_regenerate(batch_id: str, req: prompts.MarkRegenerate, ctx: ProjectContext = Depends(project),
                    s: Studio = Depends(studio)) -> dict[str, Any]:
    return {"results": prompts.mark_regenerate(s, ctx, batch_id, req)}


@router.post("/{batch_id}:regenerate")
def regenerate(batch_id: str, req: prompts.Regenerate, ctx: ProjectContext = Depends(project),
               s: Studio = Depends(studio)) -> JSONResponse:
    return _accepted(_idem(lambda: prompts.regenerate(s, ctx, batch_id, req)))


@router.post("/{batch_id}:approve-candidates")
def approve(batch_id: str, req: review.Approve, ctx: ProjectContext = Depends(project),
            s: Studio = Depends(studio)) -> dict[str, Any]:
    return _idem(lambda: review.approve(s, ctx, batch_id, req))


@router.post("/{batch_id}:clear-approval")
def clear_approval(batch_id: str, req: review.ClearApproval, ctx: ProjectContext = Depends(project),
                   s: Studio = Depends(studio)) -> dict[str, Any]:
    return {"results": review.clear_approval(s, ctx, batch_id, req)}


@router.post("/{batch_id}:preview-best")
def preview_best(batch_id: str, req: review.PreviewBest, ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    return review.preview_best(ctx, batch_id, req)


@router.post("/{batch_id}:build-approved")
def build(batch_id: str, req: production.BuildApproved, ctx: ProjectContext = Depends(project),
          s: Studio = Depends(studio)) -> JSONResponse:
    return _accepted(_idem(lambda: production.build_approved(s, ctx, batch_id, req)))


@router.post("/{batch_id}:accept-builds")
def accept(batch_id: str, req: review.AcceptBuilds, ctx: ProjectContext = Depends(project),
           s: Studio = Depends(studio)) -> dict[str, Any]:
    return _idem(lambda: review.accept_builds(s, ctx, batch_id, req))


@router.get("/{batch_id}/publish-preview")
def publish_preview(batch_id: str, ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    return {"items": production.publish_preview(ctx, batch_id)}


@router.post("/{batch_id}:publish")
def publish(batch_id: str, req: production.Publish, ctx: ProjectContext = Depends(project),
            s: Studio = Depends(studio)) -> JSONResponse:
    return _accepted(_idem(lambda: production.publish(s, ctx, batch_id, req)))
