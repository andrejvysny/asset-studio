"""Small HTTP API on ComfyUI's server for job listing, file access and selection."""
from __future__ import annotations

import mimetypes

from aiohttp import web
from server import PromptServer

from .job_io import Job, JobError
from .nodes_job import select_candidate
from .settings import OUTPUT_ROOT

routes = PromptServer.instance.routes
ALLOWED_FILE_PREFIXES = ("candidates/", "qa/", "selected/", "cutout/", "model/processed/")


def _job(request: web.Request) -> Job:
    try:
        return Job(OUTPUT_ROOT, request.match_info["job_id"])
    except JobError as e:
        raise web.HTTPNotFound(text=str(e)) from e


def _job_summary(job: Job) -> dict:
    state = job.read_json("job_state.json")
    qa = job.read_json("qa/summary.json") if job.path("qa/summary.json").is_file() else None
    return {
        "job_id": job.id,
        "state": state["state"],
        "history": state["history"],
        "request": job.read_json("request.json"),
        "candidates": sorted(p.name for p in job.path("candidates").glob("*.png")) if job.path("candidates").is_dir() else [],
        "qa": qa,
    }


@routes.get("/line_a/jobs")
async def list_jobs(_: web.Request) -> web.Response:
    jobs = []
    if OUTPUT_ROOT.is_dir():
        for d in sorted(OUTPUT_ROOT.iterdir(), reverse=True):
            if (d / "job_state.json").is_file():
                try:
                    jobs.append({"job_id": d.name, "state": Job(OUTPUT_ROOT, d.name).state})
                except JobError:
                    continue
    return web.json_response(jobs[:200])


@routes.get("/line_a/jobs/{job_id}")
async def get_job(request: web.Request) -> web.Response:
    job = _job(request)
    data = _job_summary(job)
    data["manifest"] = job.read_json("manifest.json")
    return web.json_response(data)


@routes.get("/line_a/jobs/{job_id}/file")
async def get_file(request: web.Request) -> web.StreamResponse:
    job = _job(request)
    rel = request.query.get("path", "")
    if not rel.startswith(ALLOWED_FILE_PREFIXES):
        raise web.HTTPForbidden(text="path not allowed")
    try:
        path = job.path(rel)
    except JobError as e:
        raise web.HTTPForbidden(text=str(e)) from e
    if not path.is_file():
        raise web.HTTPNotFound()
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return web.FileResponse(path, headers={"Content-Type": ctype})


@routes.post("/line_a/jobs/{job_id}/select")
async def select(request: web.Request) -> web.Response:
    job = _job(request)
    body = await request.json()
    try:
        selection = select_candidate(job, int(body["index"]))
    except (JobError, KeyError, ValueError) as e:
        raise web.HTTPBadRequest(text=str(e)) from e
    return web.json_response(selection)
