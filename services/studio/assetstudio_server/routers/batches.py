"""v1 compatibility: `/api/v1/.../batches` meant what is now a Job. Thin adapters over the Job services (one
state machine); new grouping Batches exist only in v2."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..registry import ProjectContext
from ..services import jobs as jsvc
from ..services import production, prompts, review
from ..services import runtime as runtime_svc
from ..services.records import load_job
from ..studio import Studio
from .deps import project, studio

router = APIRouter(prefix="/api/v1/projects/{project_id}/batches", tags=["v1 (Jobs, legacy name)"])


class V1CreateBatch(jsvc.CreateJob):
    enhance: bool = True  # v1 contract: creating a batch started enhancement (v2 creates are save-only)


def _accepted(body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(body, status_code=202)


def _v1_summary(s: Studio, ctx: ProjectContext, job_id: str) -> dict[str, Any]:
    return jsvc.job_summary(s, ctx, load_job(ctx.store, job_id)[0])


@router.get("")
def list_batches(ctx: ProjectContext = Depends(project), s: Studio = Depends(studio)) -> dict[str, Any]:
    return {"batches": jsvc.list_jobs(s, ctx)}


@router.post("", status_code=201)
def create(req: V1CreateBatch, ctx: ProjectContext = Depends(project), s: Studio = Depends(studio)) -> Any:
    job, created = jsvc.create_job(s, ctx, jsvc.CreateJob(**req.model_dump(exclude={"enhance"})))
    body: dict[str, Any] = {"batch": _v1_summary(s, ctx, job.id), "created": created, "enhance": None}
    if req.enhance:
        out = prompts.enqueue_enhance(s, ctx, job.id, prompts.EnhanceRequest(
            item_ids=job.item_ids, idempotency_key=f"{req.idempotency_key}:enhance"))
        body["enhance"] = {**out, "operation": {"id": out["command_id"], "kind": "enhance"}}
    return JSONResponse(body, status_code=201 if created else 200)


@router.get("/{batch_id}")
def detail(batch_id: str, ctx: ProjectContext = Depends(project), s: Studio = Depends(studio)) -> dict[str, Any]:
    job, _ = load_job(ctx.store, batch_id)
    return jsvc.job_detail(s, ctx, batch_id, runtime_svc.build_readiness(s, job.recipe_id))


@router.post("/{batch_id}:enhance")
def enhance(batch_id: str, req: prompts.EnhanceRequest, ctx: ProjectContext = Depends(project),
            s: Studio = Depends(studio)) -> JSONResponse:
    return _accepted(prompts.enqueue_enhance(s, ctx, batch_id, req, rounds=False))


@router.post("/{batch_id}:edit-prompts")
def edit_prompts(batch_id: str, req: prompts.EditPrompts, ctx: ProjectContext = Depends(project),
                 s: Studio = Depends(studio)) -> dict[str, Any]:
    return {"results": prompts.edit_prompts(s, ctx, batch_id, req, rounds=False)}


@router.post("/{batch_id}:confirm-and-generate")
def confirm(batch_id: str, req: prompts.ConfirmAndGenerate, ctx: ProjectContext = Depends(project),
            s: Studio = Depends(studio)) -> JSONResponse:
    return _accepted(prompts.confirm_and_generate(s, ctx, batch_id, req, rounds=False))


@router.post("/{batch_id}:reexport")
def reexport(batch_id: str, req: production.Reexport, ctx: ProjectContext = Depends(project),
             s: Studio = Depends(studio)) -> JSONResponse:
    return _accepted(production.reexport(s, ctx, batch_id, req))


@router.post("/{batch_id}:mark-regenerate")
def mark_regenerate(batch_id: str, req: prompts.MarkRegenerate, ctx: ProjectContext = Depends(project),
                    s: Studio = Depends(studio)) -> dict[str, Any]:
    return {"results": prompts.mark_regenerate(s, ctx, batch_id, req)}


@router.post("/{batch_id}:regenerate")
def regenerate(batch_id: str, req: prompts.Regenerate, ctx: ProjectContext = Depends(project),
               s: Studio = Depends(studio)) -> JSONResponse:
    return _accepted(prompts.regenerate(s, ctx, batch_id, req))


@router.post("/{batch_id}:approve-candidates")
def approve(batch_id: str, req: review.Approve, ctx: ProjectContext = Depends(project),
            s: Studio = Depends(studio)) -> dict[str, Any]:
    return review.approve(s, ctx, batch_id, req)


@router.post("/{batch_id}:clear-approval")
def clear_approval(batch_id: str, req: review.ClearApproval, ctx: ProjectContext = Depends(project),
                   s: Studio = Depends(studio)) -> dict[str, Any]:
    return {"results": review.clear_approval(s, ctx, batch_id, req)}


@router.post("/{batch_id}:preview-best")
def preview_best(batch_id: str, req: review.PreviewBest, ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    return review.preview_best(ctx, [batch_id], req)


@router.post("/{batch_id}:build-approved")
def build(batch_id: str, req: production.BuildApproved, ctx: ProjectContext = Depends(project),
          s: Studio = Depends(studio)) -> JSONResponse:
    return _accepted(production.build_approved(s, ctx, batch_id, req))


@router.post("/{batch_id}:accept-builds")
def accept(batch_id: str, req: review.AcceptBuilds, ctx: ProjectContext = Depends(project),
           s: Studio = Depends(studio)) -> dict[str, Any]:
    return review.accept_builds(s, ctx, batch_id, req)


@router.get("/{batch_id}/publish-preview")
def publish_preview(batch_id: str, ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    return {"items": production.publish_preview(ctx, [batch_id])}


@router.post("/{batch_id}:publish")
def publish(batch_id: str, req: production.Publish, ctx: ProjectContext = Depends(project),
            s: Studio = Depends(studio)) -> JSONResponse:
    return _accepted(production.publish(s, ctx, batch_id, req))
