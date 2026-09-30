"""Jobs: create, inspect, run, references and waiting. Gate tools live in gates.py."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .. import views
from ..annotations import DESTRUCTIVE, READ, WRITE
from ..client import StudioClient
from ..deps import Deps, idem
from . import gates
from ._common import base, fetch_job
from ._wait import evaluate, task_errors

MAX_WAIT_S = 120.0
POLL_S = 1.0
MAX_REF_BYTES = 64 * 1024 * 1024
LIST_KEYS = ("id", "alias", "title", "kind", "counts", "next_action", "waiting_on_user", "active_run", "batch")


def _run_info(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if not run:
        return None
    return {"run_id": run.get("run_id"), "plan_id": (run.get("plan") or {}).get("plan_id"),
            "tasks": len(run.get("tasks") or [])}


async def _reference_source(deps: Deps, c: StudioClient, pid: str, artifact_id: str | None, upload_id: str | None,
                            media_id: str | None, library: dict[str, Any] | None) -> dict[str, Any]:
    given = [n for n, v in (("artifact_id", artifact_id), ("upload_id", upload_id), ("media_id", media_id),
                            ("library", library)) if v]
    if len(given) != 1:
        raise ToolError("add needs exactly one of artifact_id / upload_id / media_id / library")
    if upload_id:
        name, data = deps.files.read(upload_id, MAX_REF_BYTES)
        up = await c.post(f"/api/v1/projects/{pid}/references:upload", files={"file": (name, data)})
        return {"artifact_id": up["artifact_id"]}
    return {k: v for k, v in (("artifact_id", artifact_id), ("media_id", media_id), ("library", library)) if v}


def register(mcp: FastMCP, deps: Deps) -> None:
    @mcp.tool(annotations=WRITE)
    async def create_job(ctx: Context, title: str, items: list[dict[str, Any]], category_id: str | None = None,
                         kind: str | None = None, candidate_count: int | None = None,
                         enhance_preset: Literal["conservative", "creative"] = "conservative", run: bool = True,
                         idempotency_key: str | None = None, project_id: str | None = None) -> dict[str, Any]:
        """Create a Job: one asset per item. `items`: [{name, brief, category_id?, kind?, enhance_preset?,
        references?: [{artifact_id | media_id, note?, label?}]}]. Give `category_id` (from config_get) or `kind`.
        run=true also starts prompt enhancement (no other inference); then wait_for_job(until='prompts').
        Retrying with the same idempotency_key returns the same Job."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        body = {"title": title, "items": items, "category_id": category_id, "kind": kind,
                "candidate_count": candidate_count, "enhance_preset": enhance_preset, "run": run,
                "source": deps.identity(ctx).actor[:120], "idempotency_key": idem(idempotency_key)}
        out = await c.post(f"{base(pid)}/jobs", {k: v for k, v in body.items() if v is not None})
        job = await fetch_job(c, pid, out["job"]["id"])
        return {"job": views.job_summary(job), "created": out["created"], "run": _run_info(out.get("run"))}

    @mcp.tool(annotations=READ)
    async def list_jobs(ctx: Context, include_archived: bool = False,
                        project_id: str | None = None) -> dict[str, Any]:
        """Jobs, newest first: counts per stage, next_action, whether a human gate is waiting. Use get_job for
        items, prompts and candidates."""
        c = deps.client(ctx)
        pid = await deps.project_id(c, project_id)
        jobs = (await c.get(f"{base(pid)}/jobs"))["jobs"]
        return {"jobs": [{k: j.get(k) for k in LIST_KEYS} for j in jobs
                         if include_archived or not j.get("archived_at")]}

    @mcp.tool(annotations=READ)
    async def get_job(ctx: Context, job_id: str, raw: bool = False, project_id: str | None = None) -> dict[str, Any]:
        """One Job with its items: prompt, candidates with QA status and failed checks, approval, build result,
        legal gates (`legal` lists what you may do now) and task states. raw=true returns the full REST view."""
        c = deps.client(ctx)
        job = await fetch_job(c, await deps.project_id(c, project_id), job_id)
        return job if raw else views.job_summary(job)

    @mcp.tool(annotations=WRITE)
    async def run_job(ctx: Context, job_id: str, idempotency_key: str | None = None,
                      project_id: str | None = None) -> dict[str, Any]:
        """Start (or continue) the Job: enhances prompts of items that have none. Refused with 409 when a Batch
        run already manages the Job."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        out = await c.post(f"{base(pid)}/jobs/{job_id}:run", {"idempotency_key": idem(idempotency_key)})
        return {"run": _run_info(out), "job": views.job_summary(await fetch_job(c, pid, job_id))}

    @mcp.tool(annotations=DESTRUCTIVE)
    async def cancel_job(ctx: Context, job_id: str, project_id: str | None = None) -> dict[str, Any]:
        """Cancel this Job's queued and running tasks (generation, builds). Finished results and other Jobs are
        untouched."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        out = await c.post(f"{base(pid)}/jobs/{job_id}:cancel")
        return {**out, "job": views.job_summary(await fetch_job(c, pid, job_id))}

    @mcp.tool(annotations=WRITE)
    async def item_reference(ctx: Context, job_id: str, item_id: str, action: Literal["add", "update", "remove"],
                             artifact_id: str | None = None, upload_id: str | None = None,
                             media_id: str | None = None, library: dict[str, Any] | None = None,
                             reference_id: str | None = None, note: str | None = None,
                             crop: dict[str, float] | None = None, label: str | None = None,
                             project_id: str | None = None) -> dict[str, Any]:
        """Guidance images on an item (used by the next prompt enhancement; re-enhance afterwards).
        add: exactly one of artifact_id, upload_id (from upload_file or create_upload_url), media_id (media library) or
        library={asset_id, version_id, role?}; optional note, label, crop {x,y,w,h in 0..1}.
        update: reference_id plus note and/or crop. remove: reference_id."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        it = views.select_items(await fetch_job(c, pid, job_id), "enhance", [item_id])[0]
        rev = {"expected_item_revision": it["revision"]}
        url = f"{base(pid)}/jobs/{job_id}/items/{item_id}"
        if action == "add":
            src = await _reference_source(deps, c, pid, artifact_id, upload_id, media_id, library)
            body = {**src, "note": note or "", "crop": crop, "label": label, **rev}
            res = await c.post(f"{url}:add-reference", body)
        elif not reference_id:
            raise ToolError(f"{action} needs reference_id")
        elif action == "update":
            res = await c.patch(f"{url}:update-reference", {"reference_id": reference_id, "note": note,
                                                            "crop": crop, **rev})
        else:
            res = await c.post(f"{url}:remove-reference", {"reference_id": reference_id, **rev})
        return {"result": res, "job": views.job_summary(await fetch_job(c, pid, job_id))}

    @mcp.tool(annotations=WRITE)
    async def set_item_preset(ctx: Context, job_id: str, item_id: str, preset: Literal["conservative", "creative"],
                              project_id: str | None = None) -> dict[str, Any]:
        """How freely prompt enhancement may rewrite this item's brief. Takes effect on the next enhance."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        it = views.select_items(await fetch_job(c, pid, job_id), "enhance", [item_id])[0]
        res = await c.patch(f"{base(pid)}/jobs/{job_id}/items/{item_id}:set-preset",
                            {"preset": preset, "expected_item_revision": it["revision"]})
        return {"result": res, "job": views.job_summary(await fetch_job(c, pid, job_id))}

    @mcp.tool(annotations=READ)
    async def wait_for_job(ctx: Context, job_id: str, until: Literal["prompts", "candidates", "builds", "published",
                                                                      "idle"] = "idle", timeout_s: float = 60,
                           project_id: str | None = None) -> dict[str, Any]:
        """Block until the Job has no running work AND reached `until`: prompts (every item has a prompt),
        candidates (confirmed items have candidates with QA), builds (approved items have a finished build),
        published, idle (nothing running). Returns early with `errors` when a task failed or is blocked.
        Waits at most min(timeout_s, 120); done=false on timeout is normal: call again."""
        c = deps.client(ctx)
        pid = await deps.project_id(c, project_id)
        limit = min(max(timeout_s, 0.0), MAX_WAIT_S)
        start = time.monotonic()
        while True:
            job = await fetch_job(c, pid, job_id)
            done, reason = evaluate(job, until)
            errors = task_errors(job)
            waited = round(time.monotonic() - start, 1)
            if done or errors or waited >= limit:
                if not done and errors:
                    reason = "a task failed or is blocked"
                elif not done:
                    reason += f" (timed out after {waited}s: call wait_for_job again)"
                return {"done": done, "reason": reason, "waited_s": waited, "errors": errors or None,
                        "job": views.job_summary(job)}
            await ctx.report_progress(waited, limit, reason)
            await asyncio.sleep(POLL_S)

    gates.register(mcp, deps)
