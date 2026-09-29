"""Projects, configuration, storage, runtime, operations and events."""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from assetstudio_core.config import StudioConfig, parse_config
from assetstudio_core.inheritance import name_parts, resolve, validate_semantics
from assetstudio_core.naming import render_name, slug
from assetstudio_core.recipes import RECIPES
from assetstudio_core.safeyaml import ParseError, load_yaml
from assetstudio_storage.repo import Conflict
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..errors import ApiError
from ..registry import ProjectContext
from ..services import runtime as runtime_svc
from ..services.jobs import list_jobs
from ..services.library import category_tree
from ..services.runs import list_batches
from ..services.shotlist import shot_statuses
from ..services.storage import storage_view, test_storage
from ..studio import Studio
from .deps import project, studio

router = APIRouter(prefix="/api/v1")


class CreateProject(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    root: str | None = None
    starter_qa: bool = True


class RegisterProject(BaseModel):
    root: str


@router.get("/projects")
def list_projects(s: Studio = Depends(studio)) -> dict[str, Any]:
    return {"projects": s.registry.list(), "project_roots": [str(p) for p in s.settings.project_roots]}


@router.post("/projects", status_code=201)
def create_project(req: CreateProject, s: Studio = Depends(studio)) -> dict[str, Any]:
    root = Path(req.root) if req.root else s.settings.project_roots[0] / slug(req.name)
    ctx = s.registry.create(req.name, root, req.starter_qa)
    return {"id": ctx.id, "name": ctx.name, "root": str(ctx.root)}


@router.post("/projects:register")
def register_project(req: RegisterProject, s: Studio = Depends(studio)) -> dict[str, Any]:
    ctx = s.registry.register(Path(req.root))
    return {"id": ctx.id, "name": ctx.name, "root": str(ctx.root)}


@router.get("/projects/{project_id}/summary")
def summary(ctx: ProjectContext = Depends(project), s: Studio = Depends(studio)) -> dict[str, Any]:
    jobs = list_jobs(s, ctx)
    batches = list_batches(s, ctx)
    shots = shot_statuses(ctx)
    cfg, _ = ctx.config()
    waiting = [j for j in jobs if j["waiting_on_user"]]
    active_batches = [b for b in batches if b["latest_run"] and b["latest_run"]["status"] in
                      ("running", "paused", "waiting_for_review")]
    return {
        "id": ctx.id, "name": ctx.name, "read_only": ctx.read_only, "owner": ctx.owner, "simulated": s.simulated,
        "storage": {"backend": "local", "state": "read_only" if ctx.read_only else "local", "root": str(ctx.root)},
        "counts": {"assets": ctx.index.count(), "planned": sum(1 for x in shots if x["status"] in
                                                             ("planned", "in_batch")),
                   "shots": len(shots), "jobs": len(jobs), "batches": len(batches),
                   "active_batches": len(active_batches), "categories": len(cfg.categories),
                   "recipes": len(RECIPES)},
        # A Job waiting at a gate is counted once, whether or not its Batch also waits (no double counting).
        "waiting": {"jobs": len(waiting), "batches": len(waiting),
                    "items": sum(j["counts"]["items"] for j in waiting),
                    "by_gate": {g: sum(j["by_stage"].get(g, 0) for j in waiting)
                                for g in ("prompts", "approve", "build", "publish")},
                    "detail": [{"job_id": j["id"], "batch_id": j["id"], "alias": j["alias"],
                                "next_action": j["next_action"]} for j in waiting]},
    }


# --- configuration ---------------------------------------------------------------------------------------------
class ConfigBody(BaseModel):
    yaml: str | None = Field(default=None, max_length=2_000_000)
    config: dict[str, Any] | None = None


class PatchConfig(ConfigBody):
    expected_revision: int


def _parse_body(body: ConfigBody) -> tuple[StudioConfig | None, list[dict[str, Any]]]:
    if (body.yaml is None) == (body.config is None):
        raise ApiError(400, "invalid_request", "send exactly one of yaml / config")
    data = body.config
    if body.yaml is not None:
        try:
            data = load_yaml(body.yaml)
        except ParseError as e:
            return None, [{"path": f"line {e.line}" if e.line else "(root)", "message": str(e)}]
    cfg, errors = parse_config(data)
    if cfg is None:
        return None, [e.model_dump() for e in errors]
    return cfg, [e.model_dump() for e in validate_semantics(cfg)]


def _config_view(ctx: ProjectContext) -> dict[str, Any]:
    cfg, _ = ctx.config()
    effective = {}
    for c in cfg.categories:
        res = resolve(cfg, c.id)
        effective[c.id] = {k: {"value": r.value, "mode": r.mode.value, "source": r.source} for k, r in res.items()}
        root, sub = name_parts(cfg, c.id)
        naming = res["naming"].value or "{name}"
        effective[c.id]["_naming_examples"] = [
            render_name(naming, n, root, sub, str(res["kind"].value or ""), "a")
            for n in ("Example item", "Second example")]
    return {"config": cfg.model_dump(mode="json"), "yaml": ctx.store.config_text(cfg), "revision": cfg.revision,
            "effective": effective, "categories": category_tree(ctx)}


@router.get("/projects/{project_id}/config")
def get_config(ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    return _config_view(ctx)


@router.post("/projects/{project_id}/config:validate")
def validate_config(body: ConfigBody, ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    cfg, errors = _parse_body(body)
    return {"ok": cfg is not None and not errors, "errors": errors,
            "config": cfg.model_dump(mode="json") if cfg else None}


@router.patch("/projects/{project_id}/config")
def patch_config(body: PatchConfig, ctx: ProjectContext = Depends(project), s: Studio = Depends(studio)) -> Any:
    ctx.require_writable()
    cfg, errors = _parse_body(body)
    if cfg is None or errors:
        raise ApiError(422, "invalid_config", "configuration is invalid; nothing was saved", errors)
    with ctx.store.lock:
        current, token = ctx.config()
        if current.revision != body.expected_revision:
            raise ApiError(409, "stale_config", f"configuration changed (revision {current.revision}); reload")
        if cfg.project.id != current.project.id:
            raise ApiError(422, "invalid_config", "project.id cannot change")
        removed = {c.id for c in current.categories} - {c.id for c in cfg.categories}
        used = {c for c in removed if ctx.index.query(categories={c}, limit=1)[1] or any(
            x["category_id"] == c and x["status"] != "archived" for x in shot_statuses(ctx))}
        if used:
            raise ApiError(409, "category_in_use", "archive or reclassify first: categories still referenced",
                           sorted(used))
        cfg.revision = current.revision + 1
        try:
            ctx.store.write_config(cfg, token)
        except Conflict as e:
            raise ApiError(409, "stale_config", "configuration changed on disk; reload") from e
    s.events.publish("config", project_id=ctx.id)
    return _config_view(ctx)


# --- storage / runtime ----------------------------------------------------------------------------------------
@router.get("/projects/{project_id}/storage")
def get_storage(ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    return storage_view(ctx)


@router.post("/projects/{project_id}/storage:test")
def storage_test(ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    return test_storage(ctx)


@router.post("/projects/{project_id}/storage:rebuild-index")
def rebuild_index(ctx: ProjectContext = Depends(project), s: Studio = Depends(studio)) -> dict[str, Any]:
    res = ctx.index.rebuild(ctx.store)
    s.events.publish("library", project_id=ctx.id)
    return res


@router.get("/runtime")
def get_runtime(request: Request, s: Studio = Depends(studio)) -> dict[str, Any]:
    return runtime_svc.runtime(s, getattr(request.app.state, "coordinator", None))


@router.get("/capabilities")
def capabilities(s: Studio = Depends(studio)) -> dict[str, Any]:
    return {"recipes": runtime_svc.recipe_readiness(s), "simulated": s.simulated}


@router.get("/loras")
def loras(s: Studio = Depends(studio)) -> dict[str, Any]:
    """Speed LoRAs come from the model lock; style LoRAs must be registered on this host (none bundled)."""
    from ..models import load_lock

    lock = load_lock(s.settings.config_dir)
    statuses = runtime_svc.model_statuses(s)
    speed = [{"id": k, **v, "ready": statuses.get(v["model"]) is not None and statuses[v["model"]].ready}
             for k, v in lock.get("loras", {}).items()]
    style = [{"id": k, **v} for k, v in s.extras.get("style_loras", {}).items()]
    return {"speed": speed, "style": style,
            "note": "Style LoRAs are local files registered per host with sha256, base model and licence; "
                    "none are bundled."}


@router.post("/runtime/lanes/{lane}:reset")
def reset_lane(lane: str, s: Studio = Depends(studio)) -> dict[str, Any]:
    if lane not in s.lanes:
        raise ApiError(404, "unknown_lane", lane)
    res = s.lanes[lane].reset()
    if res["ok"]:
        for op in s.journal.list(states=("blocked",)):
            if (op.error or {}).get("code") == "gpu_ownership_unknown":
                s.journal.requeue(op.id, ("blocked",))
    return {**res, "lane": s.lanes[lane].public()}


# --- operations + events --------------------------------------------------------------------------------------
@router.get("/operations")
def list_operations(project_id: str | None = None, batch_id: str | None = None,
                    active: bool = False, s: Studio = Depends(studio)) -> dict[str, Any]:
    states = ("held", "queued", "running", "cancel_requested", "reconciling", "blocked") if active else None
    return {"operations": [o.public() for o in s.journal.list(project_id=project_id, batch_id=batch_id,
                                                              states=states)]}


@router.get("/operations/{op_id}")
def get_operation(op_id: str, s: Studio = Depends(studio)) -> dict[str, Any]:
    op = s.journal.get(op_id)
    if op is None:
        raise ApiError(404, "unknown_operation", op_id)
    return op.public()


@router.post("/operations/{op_id}:cancel")
def cancel_operation(op_id: str, s: Studio = Depends(studio)) -> dict[str, Any]:
    """v1 route. Stage task ids (stk_) are cancelled as tasks; legacy op ids keep the journal semantics."""
    if op_id.startswith("stk_"):
        return cancel_task(op_id, s)
    op = s.journal.request_cancel(op_id)
    if op is None:
        raise ApiError(404, "unknown_operation", op_id)
    s.events.publish("operation", project_id=op.project_id, op_id=op.id)
    return op.public()


@router.post("/operations/{op_id}:retry")
def retry_operation(op_id: str, s: Studio = Depends(studio)) -> dict[str, Any]:
    if op_id.startswith("stk_"):
        return retry_task(op_id, s)
    op = s.journal.get(op_id)
    if op is None:
        raise ApiError(404, "unknown_operation", op_id)
    if op.state not in ("failed", "blocked") or not s.journal.requeue(op_id, ("failed", "blocked")):
        raise ApiError(409, "not_retryable", f"operation is {s.journal.get(op_id).state}")  # type: ignore[union-attr]
    s.events.publish("operation", project_id=op.project_id, op_id=op.id)
    return s.journal.get(op_id).public()  # type: ignore[union-attr]


# --- v2 stage tasks + model passes -------------------------------------------------------------------------------
v2 = APIRouter(prefix="/api/v2", tags=["tasks"])


@v2.get("/tasks")
def list_tasks(project_id: str | None = None, job_id: str | None = None, run_id: str | None = None,
               item_id: str | None = None, active: bool = False, s: Studio = Depends(studio)) -> dict[str, Any]:
    states = ("queued", "running", "blocked", "reconciling") if active else None
    return {"tasks": [t.public() for t in s.journal.tasks.list(project_id=project_id, job_id=job_id, run_id=run_id,
                                                               item_id=item_id, states=states)]}


@v2.get("/tasks/{task_id}")
def get_task(task_id: str, s: Studio = Depends(studio)) -> dict[str, Any]:
    t = s.journal.tasks.get(task_id)
    if t is None:
        raise ApiError(404, "unknown_task", task_id)
    return t.public()


@v2.post("/tasks/{task_id}:cancel")
def cancel_task(task_id: str, s: Studio = Depends(studio)) -> dict[str, Any]:
    t = s.journal.tasks.get(task_id)
    if t is None:
        raise ApiError(404, "unknown_task", task_id)
    s.journal.tasks.request_cancel([task_id])
    s.events.publish("task", project_id=t.project_id, job_id=t.job_id, item_id=t.item_id, task_id=t.id)
    return s.journal.tasks.get(task_id).public()  # type: ignore[union-attr]


@v2.post("/tasks/{task_id}:retry")
def retry_task(task_id: str, s: Studio = Depends(studio)) -> dict[str, Any]:
    """Same logical inputs again (never a regeneration); cancelled tasks are not resurrected."""
    t = s.journal.tasks.get(task_id)
    if t is None:
        raise ApiError(404, "unknown_task", task_id)
    if not s.journal.tasks.retry(task_id):
        raise ApiError(409, "not_retryable", f"task is {t.state} (control {t.control})")
    s.events.publish("task", project_id=t.project_id, job_id=t.job_id, item_id=t.item_id, task_id=t.id)
    return s.journal.tasks.get(task_id).public()  # type: ignore[union-attr]


@v2.get("/passes")
def list_passes(lane: str | None = None, limit: int = Query(default=50, le=500),
                s: Studio = Depends(studio)) -> dict[str, Any]:
    """Model passes with measured load deltas where the worker reports them (null = unavailable, never 0)."""
    return {"passes": s.journal.tasks.passes(lane=lane, limit=limit)}


@router.get("/events")
def events(request: Request, cursor: int = Query(default=-1), s: Studio = Depends(studio)) -> StreamingResponse:
    last = request.headers.get("last-event-id")
    start = int(last.split(":")[-1]) if last and last.split(":")[0] == s.events.epoch else cursor
    if start < 0:
        start = s.events.seq

    def stream() -> Iterator[str]:
        pos = start
        yield f"event: hello\ndata: {json.dumps({'epoch': s.events.epoch, 'seq': pos})}\n\n"
        for _ in range(240):  # ~1h max per connection; the client reconnects with Last-Event-ID
            batch, expired = s.events.since(pos, timeout=15.0)
            if expired:
                yield "event: reset\ndata: {}\n\n"
            for e in batch:
                pos = e["seq"]
                yield f"id: {s.events.epoch}:{pos}\nevent: change\ndata: {json.dumps(e)}\n\n"
            if not batch:
                yield ": keepalive\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
