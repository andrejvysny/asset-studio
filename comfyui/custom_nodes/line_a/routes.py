"""Small HTTP API on ComfyUI's server: job listing/detail, artifact files, bound approval."""
from __future__ import annotations

import mimetypes
import posixpath

from aiohttp import web
from server import PromptServer

from .jobcore.approval import OverrideRequired, approve
from .jobcore.job_io import Job, JobConflict, JobError
from .jobcore.views import iter_jobs, job_detail, job_summary
from .settings import OUTPUT_ROOT

routes = PromptServer.instance.routes
ALLOWED_FILE_PREFIXES = ("candidates/", "qa/", "model/attempts/")
ALLOWED_FILE_NAMES = ("manifest.json", "enhancement.json", "request.json")


def _job(request: web.Request) -> Job:
    try:
        return Job(OUTPUT_ROOT, request.match_info["job_id"])
    except JobError as e:
        raise web.HTTPNotFound(text=str(e)) from e


def clear_interrupted_operations() -> None:
    """Only ComfyUI runs job operations, so anything still 'active' at startup was interrupted."""
    for job in iter_jobs(OUTPUT_ROOT):
        try:
            job.clear_stale_operation("ComfyUI restarted")
        except (JobError, OSError, ValueError):
            continue


@routes.get("/line_a/jobs")
async def list_jobs(_: web.Request) -> web.Response:
    out = []
    for job in iter_jobs(OUTPUT_ROOT)[:500]:
        try:
            out.append(job_summary(job))
        except (JobError, OSError, ValueError, KeyError):
            continue
    return web.json_response(out)


@routes.get("/line_a/jobs/{job_id}")
async def get_job(request: web.Request) -> web.Response:
    return web.json_response(job_detail(_job(request)))


@routes.get("/line_a/jobs/{job_id}/file")
async def get_file(request: web.Request) -> web.StreamResponse:
    job = _job(request)
    rel = posixpath.normpath(request.query.get("path", "")).lstrip("/")
    if rel.startswith("..") or not (rel.startswith(ALLOWED_FILE_PREFIXES) or rel in ALLOWED_FILE_NAMES):
        raise web.HTTPForbidden(text="path not allowed")
    try:
        path = job.path(rel)
    except JobError as e:
        raise web.HTTPForbidden(text=str(e)) from e
    if not path.is_file():
        raise web.HTTPNotFound()
    ctype = mimetypes.guess_type(path.name)[0] or ("model/gltf-binary" if path.suffix == ".glb" else "application/octet-stream")
    return web.FileResponse(path, headers={"Content-Type": ctype})


@routes.post("/line_a/jobs/{job_id}/approve")
async def approve_route(request: web.Request) -> web.Response:
    """Body: {set_id: str, index: int, image_sha256: str, override?: bool}. Idempotent repeats.
    409 on stale/mismatched/busy, or {"override_required": true} when the candidate is not QA-recommended."""
    job = _job(request)
    try:
        body = await request.json()
    except ValueError as e:
        raise web.HTTPBadRequest(text="invalid JSON") from e
    set_id, index, sha = body.get("set_id"), body.get("index"), body.get("image_sha256")
    override = body.get("override", False)
    if (not isinstance(set_id, str) or not isinstance(sha, str) or isinstance(index, bool) or not isinstance(index, int)
            or not isinstance(override, bool)):
        raise web.HTTPBadRequest(text="expected {set_id: str, index: int, image_sha256: str, override?: bool}")
    try:
        attempt, created = approve(job, set_id, index, sha, override=override)
    except OverrideRequired as e:
        return web.json_response({"override_required": True, "detail": str(e)}, status=409)
    except JobConflict as e:
        raise web.HTTPConflict(text=str(e)) from e
    except JobError as e:
        raise web.HTTPBadRequest(text=str(e)) from e
    return web.json_response({"attempt": attempt, "created": created}, status=201 if created else 200)


clear_interrupted_operations()
