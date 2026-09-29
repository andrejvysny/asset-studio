"""v2 Batches (named groups of Jobs) and their runs: plan -> start -> waves across Jobs."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..registry import ProjectContext
from ..services import jobs as jsvc
from ..services import production, prompts, review, runs
from ..services.records import load_job
from ..studio import Studio
from .deps import project, studio

router = APIRouter(prefix="/api/v2/projects/{project_id}", tags=["batches"])


@router.get("/batches")
def list_batches(ctx: ProjectContext = Depends(project), s: Studio = Depends(studio)) -> dict[str, Any]:
    return {"batches": runs.list_batches(s, ctx)}


@router.post("/batches", status_code=201)
def create_batch(req: runs.CreateBatch, ctx: ProjectContext = Depends(project),
                 s: Studio = Depends(studio)) -> dict[str, Any]:
    out = runs.create_batch(s, ctx, req)
    return {"batch": runs.batch_summary(s, ctx, runs.load_batch_group(ctx, out["batch_id"])[0])}


@router.get("/batches/{batch_id}")
def batch_detail(batch_id: str, ctx: ProjectContext = Depends(project), s: Studio = Depends(studio)) -> dict:
    batch, _ = runs.load_batch_group(ctx, batch_id)
    return {**runs.batch_summary(s, ctx, batch),
            "jobs_detail": [jsvc.job_summary(s, ctx, load_job(ctx.store, j)[0]) for j in batch.job_ids],
            "run_history": [runs.run_summary(s, ctx, runs.load_run(ctx, r)[0]) for r in batch.runs]}


@router.patch("/batches/{batch_id}")
def update_batch(batch_id: str, req: runs.UpdateBatch, ctx: ProjectContext = Depends(project),
                 s: Studio = Depends(studio)) -> dict[str, Any]:
    return runs.update_batch(s, ctx, batch_id, req)


@router.post("/batches/{batch_id}:plan")
def plan(batch_id: str, req: runs.PlanRun, ctx: ProjectContext = Depends(project),
         s: Studio = Depends(studio)) -> dict[str, Any]:
    return runs.plan_run(s, ctx, batch_id, [], req.stop_at)


@router.post("/batches/{batch_id}:start")
def start(batch_id: str, req: runs.StartRun, ctx: ProjectContext = Depends(project),
          s: Studio = Depends(studio)) -> JSONResponse:
    return JSONResponse(runs.start_run(s, ctx, batch_id, req), status_code=202)


# --- runs -----------------------------------------------------------------------------------------------------------
@router.get("/runs")
def list_runs(ctx: ProjectContext = Depends(project), s: Studio = Depends(studio)) -> dict[str, Any]:
    return {"runs": [runs.run_summary(s, ctx, r) for r in reversed(runs.runs(ctx))]}


@router.get("/runs/{run_id}")
def run_detail(run_id: str, ctx: ProjectContext = Depends(project), s: Studio = Depends(studio)) -> dict:
    """Stage review across Jobs: every selected item with its Job, gates and tasks."""
    from ..services import runtime as runtime_svc

    run, _ = runs.load_run(ctx, run_id)
    jobs = []
    for jid, items in run.selection.items():
        job, _ = load_job(ctx.store, jid)
        d = jsvc.job_detail(s, ctx, jid, runtime_svc.build_readiness(s, job.recipe_id))
        d["items"] = [i for i in d["items"] if i["id"] in items]
        jobs.append(d)
    tasks = s.journal.tasks.list(project_id=ctx.id, run_id=run_id)
    passes = [p for p in s.journal.tasks.passes(limit=200) if set(p["task_ids"] or []) & {t.id for t in tasks}]
    return {**runs.run_summary(s, ctx, run), "jobs": jobs, "passes": passes,
            "tasks": [{k: v for k, v in t.public().items() if k != "progress"} for t in tasks]}


@router.post("/runs/{run_id}:confirm-prompts")
def wave_confirm(run_id: str, req: prompts.ConfirmAndGenerate, ctx: ProjectContext = Depends(project),
                 s: Studio = Depends(studio)) -> JSONResponse:
    runs.require_wave(s, ctx, run_id, req.items)
    return JSONResponse(prompts.confirm_and_generate(s, ctx, None, req, run_id), status_code=202)


@router.post("/runs/{run_id}:approve-candidates")
def wave_approve(run_id: str, req: review.Approve, ctx: ProjectContext = Depends(project),
                 s: Studio = Depends(studio)) -> dict[str, Any]:
    runs.require_wave(s, ctx, run_id, req.items)
    return review.approve(s, ctx, None, req, run_id)


@router.post("/runs/{run_id}:preview-best")
def wave_preview_best(run_id: str, req: review.PreviewBest, ctx: ProjectContext = Depends(project)) -> dict:
    run, _ = runs.load_run(ctx, run_id)
    return review.preview_best(ctx, list(run.selection), req)


@router.post("/runs/{run_id}:build-approved")
def wave_build(run_id: str, req: production.BuildApproved, ctx: ProjectContext = Depends(project),
               s: Studio = Depends(studio)) -> JSONResponse:
    runs.require_wave(s, ctx, run_id, req.items)
    return JSONResponse(production.build_approved(s, ctx, None, req, run_id), status_code=202)


@router.post("/runs/{run_id}:accept-builds")
def wave_accept(run_id: str, req: review.AcceptBuilds, ctx: ProjectContext = Depends(project),
                s: Studio = Depends(studio)) -> dict[str, Any]:
    runs.require_wave(s, ctx, run_id, req.items)
    return review.accept_builds(s, ctx, None, req, run_id)


@router.get("/runs/{run_id}/publish-preview")
def wave_publish_preview(run_id: str, ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    run, _ = runs.load_run(ctx, run_id)
    return {"items": [p for p in production.publish_preview(ctx, list(run.selection))
                      if p["item_id"] in run.selection.get(p["job_id"], {})]}


@router.post("/runs/{run_id}:publish")
def wave_publish(run_id: str, req: production.Publish, ctx: ProjectContext = Depends(project),
                 s: Studio = Depends(studio)) -> JSONResponse:
    runs.require_wave(s, ctx, run_id, req.items)
    return JSONResponse(production.publish(s, ctx, None, req, run_id), status_code=202)


@router.post("/runs/{run_id}:{action}")
def run_control(run_id: str, action: str, ctx: ProjectContext = Depends(project),
                s: Studio = Depends(studio)) -> dict[str, Any]:
    return runs.control_run(s, ctx, run_id, action)
