"""v2 Jobs: production workflows of one or more items. Create = save (no inference); runs are explicit."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..errors import ApiError
from ..registry import ProjectContext
from ..services import jobs as jsvc
from ..services import production, prompts, review, runs
from ..services import runtime as runtime_svc
from ..services.records import load_job
from ..studio import Studio
from .deps import project, studio

router = APIRouter(prefix="/api/v2/projects/{project_id}/jobs", tags=["jobs"])


class CreateJobV2(jsvc.CreateJob):
    run: bool = False  # "Save and run": explicit side effect, off by default


class RunJob(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=100)


def _accepted(body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(body, status_code=202)


def _start_standalone(s: Studio, ctx: ProjectContext, job_id: str, key: str) -> dict[str, Any]:
    """Single-Job execution goes through the same planner/scheduler as a Batch, without a visible Batch."""
    plan = runs.plan_run(s, ctx, None, [job_id])
    return runs.start_run(s, ctx, None, runs.StartRun(plan_id=plan["plan_id"], plan_sha256=plan["plan_sha256"],
                                                      idempotency_key=key)) | {"plan": plan}


@router.get("")
def list_jobs(ctx: ProjectContext = Depends(project), s: Studio = Depends(studio)) -> dict[str, Any]:
    return {"jobs": jsvc.list_jobs(s, ctx)}


@router.post("", status_code=201)
def create(req: CreateJobV2, ctx: ProjectContext = Depends(project), s: Studio = Depends(studio)) -> Any:
    job, created = jsvc.create_job(s, ctx, jsvc.CreateJob(**req.model_dump(exclude={"run"})))
    body: dict[str, Any] = {"job": jsvc.job_summary(s, ctx, job), "created": created, "run": None}
    if req.run:
        body["run"] = _start_standalone(s, ctx, job.id, f"{req.idempotency_key}:run")
    return JSONResponse(body, status_code=201 if created else 200)


@router.get("/{job_id}")
def detail(job_id: str, ctx: ProjectContext = Depends(project), s: Studio = Depends(studio)) -> dict[str, Any]:
    job, _ = load_job(ctx.store, job_id)
    return jsvc.job_detail(s, ctx, job_id, runtime_svc.build_readiness(s, job.recipe_id))


@router.post("/{job_id}:run")
def run(job_id: str, req: RunJob, ctx: ProjectContext = Depends(project), s: Studio = Depends(studio)) -> Any:
    owner = runs.active_run_for(s, ctx, job_id)
    if owner is not None:
        raise ApiError(409, "job_in_active_run", f"this Job is managed by run {owner}; continue it there", owner)
    return _accepted(_start_standalone(s, ctx, job_id, req.idempotency_key))


@router.post("/{job_id}:cancel")
def cancel(job_id: str, ctx: ProjectContext = Depends(project), s: Studio = Depends(studio)) -> dict[str, Any]:
    """Cancels this Job's pending/running tasks only; unaffected Jobs (and their results) are untouched."""
    ids = [t.id for t in s.journal.tasks.list(project_id=ctx.id, job_id=job_id, states=("queued", "running",
                                                                                     "blocked", "reconciling"))]
    done = s.journal.tasks.request_cancel(ids)
    s.events.publish("job", project_id=ctx.id, job_id=job_id)
    return {"requested": len(ids), "cancelled_before_start": len(done)}


@router.post("/{job_id}:enhance")
def enhance(job_id: str, req: prompts.EnhanceRequest, ctx: ProjectContext = Depends(project),
            s: Studio = Depends(studio)) -> JSONResponse:
    return _accepted(prompts.enqueue_enhance(s, ctx, job_id, req))


@router.post("/{job_id}:edit-prompts")
def edit_prompts(job_id: str, req: prompts.EditPrompts, ctx: ProjectContext = Depends(project),
                 s: Studio = Depends(studio)) -> dict[str, Any]:
    return {"results": prompts.edit_prompts(s, ctx, job_id, req)}


@router.post("/{job_id}:confirm-and-generate")
def confirm(job_id: str, req: prompts.ConfirmAndGenerate, ctx: ProjectContext = Depends(project),
            s: Studio = Depends(studio)) -> JSONResponse:
    return _accepted(prompts.confirm_and_generate(s, ctx, job_id, req))


@router.post("/{job_id}:mark-regenerate")
def mark_regenerate(job_id: str, req: prompts.MarkRegenerate, ctx: ProjectContext = Depends(project),
                    s: Studio = Depends(studio)) -> dict[str, Any]:
    return {"results": prompts.mark_regenerate(s, ctx, job_id, req)}


@router.post("/{job_id}:regenerate")
def regenerate(job_id: str, req: prompts.Regenerate, ctx: ProjectContext = Depends(project),
               s: Studio = Depends(studio)) -> JSONResponse:
    return _accepted(prompts.regenerate(s, ctx, job_id, req))


@router.post("/{job_id}:approve-candidates")
def approve(job_id: str, req: review.Approve, ctx: ProjectContext = Depends(project),
            s: Studio = Depends(studio)) -> dict[str, Any]:
    return review.approve(s, ctx, job_id, req)


@router.post("/{job_id}:clear-approval")
def clear_approval(job_id: str, req: review.ClearApproval, ctx: ProjectContext = Depends(project),
                   s: Studio = Depends(studio)) -> dict[str, Any]:
    return {"results": review.clear_approval(s, ctx, job_id, req)}


@router.post("/{job_id}:preview-best")
def preview_best(job_id: str, req: review.PreviewBest, ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    return review.preview_best(ctx, [job_id], req)


@router.post("/{job_id}:build-approved")
def build(job_id: str, req: production.BuildApproved, ctx: ProjectContext = Depends(project),
          s: Studio = Depends(studio)) -> JSONResponse:
    return _accepted(production.build_approved(s, ctx, job_id, req))


@router.post("/{job_id}:run-transform")
def run_transform(job_id: str, req: production.RunTransform, ctx: ProjectContext = Depends(project),
                  s: Studio = Depends(studio)) -> JSONResponse:
    return _accepted(production.run_transform(s, ctx, job_id, req))


@router.post("/{job_id}:reexport")
def reexport(job_id: str, req: production.Reexport, ctx: ProjectContext = Depends(project),
             s: Studio = Depends(studio)) -> JSONResponse:
    return _accepted(production.reexport(s, ctx, job_id, req))


@router.post("/{job_id}:retry-preview")
def retry_preview(job_id: str, req: production.RetryPreview, ctx: ProjectContext = Depends(project),
                  s: Studio = Depends(studio)) -> JSONResponse:
    return _accepted(production.retry_preview(s, ctx, job_id, req))


@router.post("/{job_id}:accept-builds")
def accept(job_id: str, req: review.AcceptBuilds, ctx: ProjectContext = Depends(project),
           s: Studio = Depends(studio)) -> dict[str, Any]:
    return review.accept_builds(s, ctx, job_id, req)


@router.get("/{job_id}/publish-preview")
def publish_preview(job_id: str, ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    return {"items": production.publish_preview(ctx, [job_id])}


@router.post("/{job_id}:publish")
def publish(job_id: str, req: production.Publish, ctx: ProjectContext = Depends(project),
            s: Studio = Depends(studio)) -> JSONResponse:
    return _accepted(production.publish(s, ctx, job_id, req))
