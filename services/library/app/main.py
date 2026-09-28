"""library-service: Asset Studio backend. Serves the web UI, library/catalog API, and proxies ComfyUI (the v1 job API).

All job mutations go through ComfyUI (/comfy/*); this service only reads output/ and owns library/assignments.json.
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import posixpath
from pathlib import Path

import httpx
import websockets
import yaml
from app import models_check
from app.assignments import AssignmentStore, completed_attempts
from app.catalog import get_catalog
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from jobcore.job_io import Job, JobConflict, JobError
from jobcore.qa_rules import configured_checks
from jobcore.views import iter_jobs, job_detail, job_summary
from pydantic import BaseModel

SRV = Path(os.environ.get("SRV_ROOT", "/srv"))
CONFIG_DIR, OUTPUT_ROOT = SRV / "config", (SRV / "output").resolve()
WEB_DIR = Path(os.environ.get("WEB_DIR", "/app/web"))
COMFY_URL = os.environ.get("COMFY_URL", "http://comfyui:8188")
WORKERS = {"prompt-service": os.environ.get("PROMPT_SERVICE_URL", "http://prompt-service:8001"),
           "trellis-worker": os.environ.get("TRELLIS_WORKER_URL", "http://trellis-worker:8002")}
WORKFLOWS = {"line_a_enhance", "line_a_generate", "line_a_3d", "line_a_reexport"}
FILE_PREFIXES = ("candidates/", "qa/", "model/attempts/")

app = FastAPI(title="line-a library-service")
store = AssignmentStore(SRV / "library", OUTPUT_ROOT)
http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5))


def catalog():  # noqa: ANN201 - cached Catalog
    return get_catalog(str(CONFIG_DIR))


def _job(job_id: str) -> Job:
    try:
        return Job(OUTPUT_ROOT, job_id)
    except JobError as e:
        raise HTTPException(404, str(e)) from e


# ---------------------------------------------------------------- library
@app.get("/api/catalog")
def get_catalog_summary() -> dict:
    assigned = set(store.all())
    return {"biomes": catalog().summary(assigned), "assigned_total": len(assigned),
            "base_total": len(catalog().slots)}


@app.get("/api/catalog/{biome}")
def get_biome(biome: str, layer: int | None = None) -> dict:
    if biome not in catalog().conv["biomes"]:
        raise HTTPException(404, f"unknown biome {biome}")
    return catalog().biome_tree(biome, store.all(), layer)


@app.get("/api/coverage")
def get_coverage() -> dict:
    return catalog().coverage(set(store.all()))


@app.get("/api/slots/{slot_id}")
def get_slot(slot_id: str) -> dict:
    if slot_id not in catalog().slots:
        raise HTTPException(404, f"unknown slot {slot_id}")
    detail = catalog().slot_detail(slot_id)
    assignment = store.all().get(slot_id)
    manifest = None
    if assignment:
        try:
            manifest = _job(assignment["job_id"]).read_json("manifest.json")
        except (HTTPException, OSError, ValueError):
            manifest = None
    return {**detail, "assignment": assignment, "manifest": manifest}


class AssignBody(BaseModel):
    job_id: str
    attempt_id: str


@app.put("/api/slots/{slot_id}/assignment")
def put_assignment(slot_id: str, body: AssignBody) -> dict:
    if slot_id not in catalog().slots:
        raise HTTPException(404, f"unknown slot {slot_id}")
    try:
        return store.assign(slot_id, body.job_id, body.attempt_id)
    except JobConflict as e:
        raise HTTPException(409, str(e)) from e
    except (JobError, FileNotFoundError) as e:
        raise HTTPException(404, str(e)) from e


@app.delete("/api/slots/{slot_id}/assignment")
def delete_assignment(slot_id: str) -> dict:
    return {"removed": store.unassign(slot_id)}


@app.get("/api/assets")
def get_assets(unassigned: bool = False) -> list[dict]:
    assigned = {(a["job_id"], a["attempt_id"]): s for s, a in store.all().items()}
    out = [{**a, "slot": assigned.get((a["job_id"], a["attempt_id"]))} for a in completed_attempts(iter_jobs(OUTPUT_ROOT))]
    return [a for a in out if not a["slot"]] if unassigned else out


# ---------------------------------------------------------------- jobs (read-only views)
@app.get("/api/jobs")
def get_jobs() -> list[dict]:
    out = []
    for job in iter_jobs(OUTPUT_ROOT)[:500]:
        try:
            out.append(job_summary(job))
        except (JobError, OSError, ValueError, KeyError):
            continue
    return out


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = _job(job_id)
    detail = job_detail(job)
    detail["summary"] = job_summary(job)
    detail["slots"] = {a["attempt_id"]: s for s, a in store.all().items() if a["job_id"] == job_id}
    return detail


@app.get("/api/files/{job_id}/{rel:path}")
def get_file(job_id: str, rel: str) -> FileResponse:
    job = _job(job_id)
    rel = posixpath.normpath(rel).lstrip("/")
    if rel.startswith("..") or not rel.startswith(FILE_PREFIXES):
        raise HTTPException(403, "path not allowed")
    try:
        path = job.path(rel)
    except JobError as e:
        raise HTTPException(403, str(e)) from e
    if not path.is_file():
        raise HTTPException(404)
    ctype = "model/gltf-binary" if path.suffix == ".glb" else mimetypes.guess_type(path.name)[0]
    return FileResponse(path, media_type=ctype or "application/octet-stream")


@app.get("/api/workflows/{name}")
def get_workflow(name: str) -> JSONResponse:
    if name not in WORKFLOWS:
        raise HTTPException(404, f"unknown workflow {name}")
    return JSONResponse(json.loads((CONFIG_DIR / "workflows" / f"{name}.api.json").read_text()))


@app.get("/api/config")
def get_config() -> dict:
    cfg = yaml.safe_load((CONFIG_DIR / "app.yaml").read_text())
    tpl = (CONFIG_DIR / "prompts" / "model_sheet_template.txt").read_text().strip()
    rules = yaml.safe_load((CONFIG_DIR / "qa" / "rules.yaml").read_text())
    checks = [{"id": k, **v} for k, v in configured_checks(rules).items()]
    return {"image": cfg["image"], "mesh": cfg["mesh"], "pipeline": cfg["pipeline"], "template": tpl,
            "qa_checks": checks, "minor_fail_limit": rules["minor_fail_limit"]}


# ---------------------------------------------------------------- runtime
async def _get_json(url: str) -> dict:
    try:
        r = await http.get(url, timeout=5)
        return {"reachable": True, **r.json()}
    except (httpx.HTTPError, ValueError) as e:
        return {"reachable": False, "error": str(e)[:200]}


@app.get("/api/runtime")
async def get_runtime() -> dict:
    comfy, *workers = await asyncio.gather(_get_json(f"{COMFY_URL}/system_stats"),
                                           *[_get_json(f"{u}/health") for u in WORKERS.values()])
    queue = await _get_json(f"{COMFY_URL}/queue")
    return {
        "comfyui": comfy,
        "queue": {"running": len(queue.get("queue_running", [])), "pending": len(queue.get("queue_pending", []))},
        "workers": dict(zip(WORKERS, workers)),
        "models": models_check.check_all(SRV),
        "licences": yaml.safe_load((CONFIG_DIR / "licences.yaml").read_text())["components"],
    }


# ---------------------------------------------------------------- ComfyUI proxy (single origin for the SPA)
@app.websocket("/comfy/ws")
async def comfy_ws(ws: WebSocket) -> None:
    await ws.accept()
    target = COMFY_URL.replace("http", "ws", 1) + "/ws?" + str(ws.query_params)
    async with websockets.connect(target, max_size=None) as upstream:
        async def down() -> None:
            async for msg in upstream:
                await (ws.send_bytes(msg) if isinstance(msg, bytes) else ws.send_text(msg))

        async def up() -> None:
            while True:
                await upstream.send(await ws.receive_text())

        done, pending = await asyncio.wait([asyncio.create_task(down()), asyncio.create_task(up())],
                                           return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()


@app.api_route("/comfy/{path:path}", methods=["GET", "POST", "DELETE"])
async def comfy_proxy(path: str, request: Request) -> Response:
    try:
        r = await http.request(request.method, f"{COMFY_URL}/{path}", params=request.query_params,
                               content=await request.body(),
                               headers={"Content-Type": request.headers.get("content-type", "application/json")})
    except httpx.HTTPError as e:
        raise HTTPException(502, f"ComfyUI unreachable: {e}") from e
    return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))


# ---------------------------------------------------------------- SPA
@app.get("/{path:path}")
def spa(path: str) -> FileResponse:
    candidate = (WEB_DIR / path).resolve()
    if path and candidate.is_file() and candidate.is_relative_to(WEB_DIR.resolve()):
        return FileResponse(candidate)
    index = WEB_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(404, "web UI not built")
    return FileResponse(index)
