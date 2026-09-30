"""Batches (named groups of Jobs) and their runs: plan/start, cross-Job gate waves, pause/resume/cancel/close."""
from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .. import views
from ..annotations import DESTRUCTIVE, READ, WRITE
from ..client import StudioClient
from ..deps import Deps, idem
from ._common import base, check_results, unwrap
from .gates import BINDING_FIELDS

Gate = Literal["confirm", "approve_best", "build", "accept", "publish"]
# gate -> (views gate for default selection, wave route)
WAVES: dict[str, tuple[str, str]] = {
    "confirm": ("confirm", "confirm-prompts"), "build": ("build", "build-approved"),
    "accept": ("accept", "accept-builds"),
}
JOB_KEYS = ("id", "alias", "title", "kind", "counts", "next_action", "waiting_on_user")


def _unit(gate: str, it: views.Item) -> dict[str, Any]:
    if gate == "confirm":
        return views.confirm_unit(it)
    if gate == "build":
        return views.build_unit(it)
    return views.build_ref(it)


def _wanted(item_ids: list[dict[str, str]] | None) -> dict[str, list[str]] | None:
    if not item_ids:
        return None
    out: dict[str, list[str]] = {}
    for ref in item_ids:
        if "job_id" not in ref or "item_id" not in ref:
            raise ToolError("item_ids entries need {job_id, item_id}")
        out.setdefault(ref["job_id"], []).append(ref["item_id"])
    return out


def _run_units(run: dict[str, Any], gate: str, item_ids: list[dict[str, str]] | None) -> list[dict[str, Any]]:
    """Units across the run's Jobs. Default selection skips Jobs with nothing ready; explicit ids must exist."""
    wanted = _wanted(item_ids)
    views_gate = WAVES[gate][0]
    units: list[dict[str, Any]] = []
    for job in run["jobs"]:
        if wanted is not None and job["id"] not in wanted:
            continue
        try:
            items = views.select_items(job, views_gate, wanted[job["id"]] if wanted else None)
        except ToolError as e:
            if wanted is not None:
                raise
            if "no items are ready" not in str(e):
                raise
            continue
        units += [{"job_id": job["id"], **_unit(gate, it)} for it in items]
    if not units:
        states = "; ".join(f"{j['id']}: {j['counts']}" for j in run["jobs"])
        raise ToolError(f"no items are ready for '{gate}' in this run: {states}")
    return units


def _run_view(run: dict[str, Any]) -> dict[str, Any]:
    failed = [t for t in run.get("tasks", []) if t.get("state") in ("failed", "blocked")]
    return {**{k: v for k, v in run.items() if k not in ("jobs", "passes", "tasks")},
            "jobs": [views.job_summary(j) for j in run["jobs"]],
            "failed_tasks": [{k: t.get(k) for k in ("id", "job_id", "item_id", "stage", "state", "error")}
                             for t in failed] or None}


async def _wave(c: StudioClient, pid: str, run_id: str, gate: str, item_ids: list[dict[str, str]] | None,
                key: str | None) -> dict[str, Any]:
    run = await c.get(f"{base(pid)}/runs/{run_id}")
    if gate == "publish":
        rows = (await c.get(f"{base(pid)}/runs/{run_id}/publish-preview"))["items"]
        wanted = _wanted(item_ids)
        units = [{"job_id": r["job_id"], "item_id": r["item_id"], "build_run_id": r["build_run_id"],
                  "expected_item_revision": r["expected_item_revision"]} for r in rows if not r["published"]
                 and (wanted is None or r["item_id"] in wanted.get(r["job_id"], []))]
        if not units:
            raise ToolError(f"no items are ready for 'publish' in run {run_id}: accept builds first")
        route = "publish"
    elif gate == "approve_best":
        return await _approve_best(c, pid, run_id, item_ids, key)
    else:
        units, route = _run_units(run, gate, item_ids), WAVES[gate][1]
    res = await c.post(f"{base(pid)}/runs/{run_id}:{route}", {"items": units, "idempotency_key": idem(key)})
    check_results(unwrap(res))
    return {"results": unwrap(res), "run": _run_view(await c.get(f"{base(pid)}/runs/{run_id}"))}


async def _approve_best(c: StudioClient, pid: str, run_id: str, item_ids: list[dict[str, str]] | None,
                        key: str | None) -> dict[str, Any]:
    wanted = _wanted(item_ids)
    prev = await c.post(f"{base(pid)}/runs/{run_id}:preview-best",
                        {"item_ids": [i for ids in wanted.values() for i in ids] if wanted else None})
    proposals = [p for p in prev["proposals"] if wanted is None or p["item_id"] in wanted.get(p["job_id"], [])]
    if not proposals:
        raise ToolError(f"no items are ready for 'approve_best' in run {run_id}: {prev['skipped']}")
    units = [{"job_id": p["job_id"], **{k: p[k] for k in BINDING_FIELDS}} for p in proposals]
    res = await c.post(f"{base(pid)}/runs/{run_id}:approve-candidates",
                       {"items": units, "idempotency_key": idem(key)})
    check_results(res["results"])
    return {"proposals": proposals, "skipped": prev["skipped"], "results": res["results"],
            "run": _run_view(await c.get(f"{base(pid)}/runs/{run_id}"))}


def register(mcp: FastMCP, deps: Deps) -> None:
    @mcp.tool(annotations=WRITE)
    async def create_batch(ctx: Context, name: str, job_ids: list[str], idempotency_key: str | None = None,
                           project_id: str | None = None) -> dict[str, Any]:
        """Group existing Jobs (create_job with run=false) under a named Batch so they can be run together
        with start_batch and moved through each gate with run_gate."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        return await c.post(f"{base(pid)}/batches", {"title": name, "job_ids": job_ids,
                                                     "idempotency_key": idem(idempotency_key)})

    @mcp.tool(annotations=WRITE)
    async def update_batch(ctx: Context, batch_id: str, name: str | None = None, job_ids: list[str] | None = None,
                           project_id: str | None = None) -> dict[str, Any]:
        """Rename a Batch and/or replace its Job list (the full new list). Uses the current revision."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        cur = await c.get(f"{base(pid)}/batches/{batch_id}")
        return await c.patch(f"{base(pid)}/batches/{batch_id}",
                             {"expected_revision": cur["revision"], "title": name, "job_ids": job_ids})

    @mcp.tool(annotations=READ)
    async def list_batches(ctx: Context, project_id: str | None = None) -> dict[str, Any]:
        """Batches with job counts and the latest run's progress counters."""
        c = deps.client(ctx)
        return await c.get(f"{base(await deps.project_id(c, project_id))}/batches")

    @mcp.tool(annotations=READ)
    async def get_batch(ctx: Context, batch_id: str, project_id: str | None = None) -> dict[str, Any]:
        """One Batch: its Jobs (counts, next_action) and run history."""
        c = deps.client(ctx)
        out = await c.get(f"{base(await deps.project_id(c, project_id))}/batches/{batch_id}")
        jobs = [{k: j.get(k) for k in JOB_KEYS} for j in out.pop("jobs_detail", [])]
        return {**out, "jobs_detail": jobs}

    @mcp.tool(annotations=WRITE)
    async def start_batch(ctx: Context, batch_id: str, idempotency_key: str | None = None,
                          project_id: str | None = None) -> dict[str, Any]:
        """Plan and start a run of the Batch: enhances prompts of every item that has none, then stops at prompt
        review. Returns the plan summary and run_id; continue with get_run / run_gate."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        plan = await c.post(f"{base(pid)}/batches/{batch_id}:plan", {})
        run = await c.post(f"{base(pid)}/batches/{batch_id}:start", {
            "plan_id": plan["plan_id"], "plan_sha256": plan["plan_sha256"], "idempotency_key": idem(idempotency_key)})
        return {"plan": {"plan_id": plan["plan_id"], "counts": plan["counts"], "preflight": plan["preflight"]},
                "run_id": run["run_id"], "skipped": run.get("skipped")}

    @mcp.tool(annotations=READ)
    async def get_run(ctx: Context, run_id: str, project_id: str | None = None) -> dict[str, Any]:
        """A run across Jobs: counters per gate, each Job's items (as in get_job) and failed tasks."""
        c = deps.client(ctx)
        return _run_view(await c.get(f"{base(await deps.project_id(c, project_id))}/runs/{run_id}"))

    @mcp.tool(annotations=WRITE)
    async def run_gate(ctx: Context, run_id: str, gate: Gate, item_ids: list[dict[str, str]] | None = None,
                       idempotency_key: str | None = None, project_id: str | None = None) -> dict[str, Any]:
        """Pass one gate for items across the Jobs of a run: confirm (prompts, starts generation), approve_best
        (best recommended candidate per item), build, accept, publish. `item_ids`: [{job_id, item_id}] or omit
        for every item ready at that gate. Generation and builds are asynchronous: poll get_run or use
        wait_for_job per Job between gates."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        return await _wave(c, pid, run_id, gate, item_ids, idempotency_key)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def run_control(ctx: Context, run_id: str, action: Literal["pause", "resume", "cancel", "close"],
                          project_id: str | None = None) -> dict[str, Any]:
        """pause: finish running tasks, start nothing new. resume: continue a paused run. cancel: cancel only
        this run's tasks (no data deleted). close: end the run (only when nothing is active)."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        return await c.post(f"{base(pid)}/runs/{run_id}:{action}")
