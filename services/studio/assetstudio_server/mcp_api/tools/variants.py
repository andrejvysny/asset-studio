"""Asset variants: capabilities, drafts (wizard state), planning helpers, plan -> Jobs, diversity reports."""
from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ..annotations import READ, WRITE
from ..client import StudioClient
from ..deps import Deps, idem

DraftAction = Literal["prepare_references", "analyze_source", "suggest_plan", "apply_suggestion", "analysis"]
ACTION_ROUTES = {"prepare_references": "prepare-references", "analyze_source": "analyze-source",
                 "suggest_plan": "suggest-plan", "apply_suggestion": "apply-suggestion"}


def _v1(pid: str) -> str:
    return f"/api/v1/projects/{pid}"


async def _revision(c: StudioClient, pid: str, draft_id: str) -> int:
    return int((await c.get(f"{_v1(pid)}/variant-drafts/{draft_id}"))["revision"])


def register(mcp: FastMCP, deps: Deps) -> None:
    @mcp.tool(annotations=READ)
    async def variant_capabilities(ctx: Context, asset_id: str, version_id: str,
                                   project_id: str | None = None) -> dict[str, Any]:
        """Which variant methods a published asset version supports (deterministic transform, source-conditioned
        edit, ...) and why others are unavailable. Call before variant_draft."""
        c = deps.client(ctx)
        pid = await deps.project_id(c, project_id)
        return await c.get(f"{_v1(pid)}/assets/{asset_id}/versions/{version_id}/variant-capabilities")

    @mcp.tool(annotations=WRITE)
    async def variant_draft(ctx: Context, action: Literal["create", "update", "get"], draft_id: str | None = None,
                            body: dict[str, Any] | None = None, idempotency_key: str | None = None,
                            project_id: str | None = None) -> dict[str, Any]:
        """Variant draft = editable plan for variants of one asset version. Saving runs no inference.
        create body: {asset_id, version_id, method, intent?, requested_variants?, candidates_per_variant?,
        change_request?, rows?: [{label, change_request?, final_height_m?, glb_transform?, raster_transform?,
        candidate_count?}], family_name?}. update body (draft_id; revision is filled in): any of {method, intent,
        preserve, rows (full list), candidates_per_row, request, family_name, primary_view, style_ack}.
        get returns the draft with capabilities, work summary and task states."""
        read_only = action == "get"
        c = deps.client(ctx, write=not read_only)
        pid = await deps.project_id(c, project_id)
        if action == "create":
            payload = {**(body or {}), "idempotency_key": idem(idempotency_key)}
            return await c.post(f"{_v1(pid)}/variant-drafts", payload)
        if not draft_id:
            raise ToolError(f"{action} needs draft_id")
        if read_only:
            return await c.get(f"{_v1(pid)}/variant-drafts/{draft_id}")
        payload = {"expected_revision": await _revision(c, pid, draft_id), **(body or {})}
        return await c.patch(f"{_v1(pid)}/variant-drafts/{draft_id}", payload)

    @mcp.tool(annotations=WRITE)
    async def variant_draft_action(ctx: Context, draft_id: str, action: DraftAction, body: dict[str, Any] | None = None,
                                   idempotency_key: str | None = None,
                                   project_id: str | None = None) -> dict[str, Any]:
        """Planning steps on a draft. prepare_references: render source views used as guidance. analyze_source:
        describe the source (asynchronous; poll variant_draft get). suggest_plan: body {count, request?} proposes
        rows (asynchronous). apply_suggestion: body {mode: 'replace_empty'|'append', indices?}. analysis: read
        the stored analysis. Use create_variant_jobs when the rows are final."""
        c = deps.client(ctx, write=action != "analysis")
        pid = await deps.project_id(c, project_id)
        url = f"{_v1(pid)}/variant-drafts/{draft_id}"
        if action == "analysis":
            return await c.get(f"{url}/analysis")
        payload: dict[str, Any] | None = None
        if action in ("analyze_source", "suggest_plan"):
            payload = {**(body or {}), "idempotency_key": idem(idempotency_key)}
        elif action == "apply_suggestion":
            payload = {"expected_revision": await _revision(c, pid, draft_id), **(body or {})}
        return await c.post(f"{url}:{ACTION_ROUTES[action]}", payload)

    @mcp.tool(annotations=WRITE)
    async def create_variant_jobs(ctx: Context, draft_id: str, idempotency_key: str | None = None,
                                  project_id: str | None = None) -> dict[str, Any]:
        """Turn the draft's rows into Jobs (one per variant) grouped in a Batch and family. Nothing runs yet:
        continue with list_jobs / run_job or start_batch; direct transforms use run_transform."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        return await c.post(f"{_v1(pid)}/variant-drafts/{draft_id}:create-jobs", {
            "expected_revision": await _revision(c, pid, draft_id), "idempotency_key": idem(idempotency_key)})

    @mcp.tool(annotations=WRITE)
    async def compare_variants(ctx: Context, plan_id: str, idempotency_key: str | None = None,
                               project_id: str | None = None) -> dict[str, Any]:
        """Start the advisory diversity comparison of the variant plan's currently selected candidates
        (asynchronous; read the result with get_diversity). Approves nothing."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        return await c.post(f"{_v1(pid)}/variant-plans/{plan_id}:compare-selection",
                            {"idempotency_key": idem(idempotency_key)})

    @mcp.tool(annotations=READ)
    async def get_diversity(ctx: Context, plan_id: str, project_id: str | None = None) -> dict[str, Any]:
        """Latest diversity report for a variant plan (which selected variants look too alike)."""
        c = deps.client(ctx)
        pid = await deps.project_id(c, project_id)
        return await c.get(f"{_v1(pid)}/variant-plans/{plan_id}/diversity")
