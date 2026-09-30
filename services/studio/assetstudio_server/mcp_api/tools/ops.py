"""Runtime tasks, model passes, lanes, storage, and the studio_api escape hatch."""
from __future__ import annotations

import re
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ..annotations import DESTRUCTIVE, READ, WRITE
from ..deps import Deps

API_PATH = re.compile(r"^/api/v[12]/")


def check_api_path(path: str) -> None:
    if not API_PATH.match(path) or ".." in path or "?" in path or "#" in path:
        raise ToolError(f"refused: {path!r} must match /api/v1/... or /api/v2/... (no '..', no query string)")


def register(mcp: FastMCP, deps: Deps) -> None:
    @mcp.tool(annotations=READ)
    async def list_tasks(ctx: Context, project_id: str | None = None, job_id: str | None = None,
                         run_id: str | None = None, item_id: str | None = None,
                         active: bool = False) -> dict[str, Any]:
        """Stage tasks (enhance, generate, qa, build, publish) with state and error. `active` = queued, running,
        blocked or reconciling only. Filters combine; all omitted lists every project's tasks."""
        c = deps.client(ctx)
        return await c.get("/api/v2/tasks", project_id=project_id, job_id=job_id, run_id=run_id, item_id=item_id,
                           active=active or None)

    @mcp.tool(annotations=READ)
    async def get_task(ctx: Context, task_id: str) -> dict[str, Any]:
        """One task: state, inputs, progress, error and retry history."""
        return await deps.client(ctx).get(f"/api/v2/tasks/{task_id}")

    @mcp.tool(annotations=DESTRUCTIVE)
    async def task_control(ctx: Context, task_id: str, action: Literal["retry", "cancel"]) -> dict[str, Any]:
        """retry: rerun a failed or blocked task with the same inputs (never a regeneration). cancel: stop a
        queued or running task."""
        return await deps.client(ctx, write=True).post(f"/api/v2/tasks/{task_id}:{action}")

    @mcp.tool(annotations=READ)
    async def list_passes(ctx: Context, lane: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Recent model passes (which model ran on which lane, with measured load times where reported)."""
        return await deps.client(ctx).get("/api/v2/passes", lane=lane, limit=min(max(limit, 1), 500))

    @mcp.tool(annotations=DESTRUCTIVE)
    async def reset_lane(ctx: Context, lane: str) -> dict[str, Any]:
        """Reset a GPU lane (unload its model, clear a stuck worker). Interrupts what the lane is running;
        use only when tasks on it are stuck. Lane names are listed by runtime_status."""
        return await deps.client(ctx, write=True).post(f"/api/v1/runtime/lanes/{lane}:reset")

    @mcp.tool(annotations=WRITE)
    async def rebuild_index(ctx: Context, project_id: str | None = None) -> dict[str, Any]:
        """Rebuild the project's search index from its stored records (safe; use when search results look stale)."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        return await c.post(f"/api/v1/projects/{pid}/storage:rebuild-index")

    @mcp.tool(annotations=READ)
    async def storage_status(ctx: Context, project_id: str | None = None) -> dict[str, Any]:
        """Storage backend, asset/version/blob counts and sizes of the project."""
        c = deps.client(ctx)
        pid = await deps.project_id(c, project_id)
        return await c.get(f"/api/v1/projects/{pid}/storage")

    @mcp.tool(annotations=DESTRUCTIVE)
    async def studio_api(ctx: Context, method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"], path: str,
                         body: Any = None, query: dict[str, Any] | None = None) -> Any:
        """Escape hatch: call a Studio REST route directly. Use only when no dedicated tool exists; the route
        keeps its own validation, revision checks and idempotency_key requirements, and non-GET calls need a
        full-scope token. `path` must start with /api/v1/ or /api/v2/ (no '..'; put parameters in `query`).
        /openapi.json is not reachable, so the route families: /api/v1/projects[/{pid}/...] (summary, config,
        assets, artifacts, media, reference-sets, shots, imports, storage), /api/v2/projects/{pid}/jobs[/{job}:gate],
        /api/v2/projects/{pid}/batches and /runs, /api/v1/projects/{pid}/variant-drafts and /variant-plans,
        /api/v2/tasks, /api/v2/passes, /api/v1/runtime, /api/v1/operations. Action routes are `<id>:<action>`."""
        check_api_path(path)
        c = deps.client(ctx, write=method != "GET")
        return await c.call(method, path, json=body, params=query)
