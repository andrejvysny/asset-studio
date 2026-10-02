"""Job gate tools: each resolves its binding from the Job's current state, then calls the Job-level REST route."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .. import views
from ..annotations import DESTRUCTIVE, WRITE
from ..client import StudioClient
from ..deps import Deps, idem
from ._common import base, check_results, fetch_job, unwrap

UnitFn = Callable[[views.Item], dict[str, Any]]


async def run_gate(c: StudioClient, pid: str, job_id: str, *, gate: str, item_ids: list[str] | None, route: str,
                   unit: UnitFn, key: str | None, keyed: bool = True) -> dict[str, Any]:
    """Job detail -> selected items -> units -> POST `:route` -> results + fresh Job summary."""
    job = await fetch_job(c, pid, job_id)
    units = [unit(it) for it in views.select_items(job, gate, item_ids)]
    body: dict[str, Any] = {"items": units}
    if keyed:
        body["idempotency_key"] = idem(key)
    return await finish(c, pid, job_id, await c.post(f"{base(pid)}/jobs/{job_id}:{route}", body))


async def finish(c: StudioClient, pid: str, job_id: str, response: Any) -> dict[str, Any]:
    results = unwrap(response)
    check_results(results)
    return {"results": results, "job": views.job_summary(await fetch_job(c, pid, job_id))}


def register(mcp: FastMCP, deps: Deps) -> None:
    async def _ctx(ctx: Context, project_id: str | None) -> tuple[StudioClient, str]:
        c = deps.client(ctx, write=True)
        return c, await deps.project_id(c, project_id)

    @mcp.tool(annotations=WRITE)
    async def enhance_prompts(ctx: Context, job_id: str, item_ids: list[str] | None = None,
                              idempotency_key: str | None = None, project_id: str | None = None) -> dict[str, Any]:
        """Generate (or regenerate) the enhanced prompt from the brief. Asynchronous: follow with
        wait_for_job(until='prompts'). Default items: those without a prompt yet."""
        c, pid = await _ctx(ctx, project_id)
        job = await fetch_job(c, pid, job_id)
        ids = [it["id"] for it in views.select_items(job, "enhance", item_ids)]
        res = await c.post(f"{base(pid)}/jobs/{job_id}:enhance",
                           {"item_ids": ids, "idempotency_key": idem(idempotency_key)})
        return {"results": res, "job": views.job_summary(await fetch_job(c, pid, job_id))}

    @mcp.tool(annotations=WRITE)
    async def edit_prompt(ctx: Context, job_id: str, item_id: str, description: str,
                          variant_index: int | None = None, project_id: str | None = None) -> dict[str, Any]:
        """Replace an item's prompt with your own description (a new prompt revision, not yet confirmed).
        Items enhanced into several prompt variants (one per candidate slot, see the item's `prompt_variants`)
        take `variant_index` to edit that slot only; without it the first prompt is edited.
        Follow with confirm_prompts. Refused once candidates exist: use regenerate(description=...) then."""
        c, pid = await _ctx(ctx, project_id)
        job = await fetch_job(c, pid, job_id)
        it = views.select_items(job, "enhance", [item_id])[0]
        body = {"items": [{**views.item_ref(it), "description": description, "variant_index": variant_index}]}
        return await finish(c, pid, job_id, await c.post(f"{base(pid)}/jobs/{job_id}:edit-prompts", body))

    @mcp.tool(annotations=WRITE)
    async def confirm_prompts(ctx: Context, job_id: str, item_ids: list[str] | None = None,
                              prompt_revision_id: str | None = None, idempotency_key: str | None = None,
                              project_id: str | None = None) -> dict[str, Any]:
        """Gate 1: confirm the current prompt and start candidate generation (asynchronous; follow with
        wait_for_job(until='candidates')). Default items: prompts awaiting confirmation. `prompt_revision_id`
        pins the exact prompt you reviewed (409 stale if it changed); it needs a single item."""
        if prompt_revision_id and (item_ids is None or len(item_ids) != 1):
            raise ToolError("prompt_revision_id needs exactly one item in item_ids")
        c, pid = await _ctx(ctx, project_id)
        return await run_gate(c, pid, job_id, gate="confirm", item_ids=item_ids, route="confirm-and-generate",
                              unit=lambda it: views.confirm_unit(it, prompt_revision_id), key=idempotency_key)

    @mcp.tool(annotations=WRITE)
    async def regenerate(ctx: Context, job_id: str, item_ids: list[str], description: str | None = None,
                         idempotency_key: str | None = None, project_id: str | None = None) -> dict[str, Any]:
        """Generate a new round of candidates (earlier rounds stay approvable). `description` optionally replaces
        the prompt for the new round. item_ids are required."""
        if not item_ids:
            raise ToolError("item_ids required")
        c, pid = await _ctx(ctx, project_id)
        return await run_gate(c, pid, job_id, gate="confirm", item_ids=item_ids, route="regenerate",
                              unit=lambda it: {**views.item_ref(it), "description": description},
                              key=idempotency_key)

    @mcp.tool(annotations=WRITE)
    async def approve_candidate(ctx: Context, job_id: str, item_id: str, candidate_id: str, override_qa: bool = False,
                                override_reason: str | None = None, idempotency_key: str | None = None,
                                project_id: str | None = None) -> dict[str, Any]:
        """Gate 2: approve one candidate (from get_job items[].candidates; earlier rounds also work). The exact
        image hash, prompt and QA evaluation are bound automatically. A candidate whose QA is not
        'recommended' needs override_qa=true plus a reason; say why you chose it."""
        c, pid = await _ctx(ctx, project_id)
        return await run_gate(c, pid, job_id, gate="approve", item_ids=[item_id], route="approve-candidates",
                              unit=lambda it: views.approve_unit(it, candidate_id, override_qa, override_reason),
                              key=idempotency_key)

    @mcp.tool(annotations=WRITE)
    async def approve_best(ctx: Context, job_id: str, item_ids: list[str] | None = None,
                           idempotency_key: str | None = None, project_id: str | None = None) -> dict[str, Any]:
        """Gate 2 shortcut: approve the best QA-recommended candidate per item (fewest failed checks). Items
        without a recommended candidate are listed in `skipped`: inspect and use approve_candidate for those."""
        c, pid = await _ctx(ctx, project_id)
        return await approve_proposals(c, pid, job_id, item_ids, idempotency_key)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def clear_approval(ctx: Context, job_id: str, item_ids: list[str] | None = None,
                             project_id: str | None = None) -> dict[str, Any]:
        """Withdraw approvals so another candidate can be chosen (refused while building or once accepted)."""
        if not item_ids:
            raise ToolError("item_ids required")
        c, pid = await _ctx(ctx, project_id)
        return await run_gate(c, pid, job_id, gate="build", item_ids=item_ids, route="clear-approval",
                              unit=views.item_ref, key=None, keyed=False)

    @mcp.tool(annotations=WRITE)
    async def build(ctx: Context, job_id: str, item_ids: list[str] | None = None,
                    mode: Literal["build", "retry", "resample", "rebuild"] = "build",
                    overrides: dict[str, Any] | None = None, idempotency_key: str | None = None,
                    project_id: str | None = None) -> dict[str, Any]:
        """Gate 3: build the approved candidate (3D model, sprite, ...). Asynchronous; follow with
        wait_for_job(until='builds'), then inspect items[].build (result, failed_checks). `mode`: build | retry
        (same inputs after failure) | resample | rebuild (with `overrides`, keys per recipe)."""
        c, pid = await _ctx(ctx, project_id)
        return await run_gate(c, pid, job_id, gate="build", item_ids=item_ids, route="build-approved",
                              unit=lambda it: views.build_unit(it, mode, overrides), key=idempotency_key)

    @mcp.tool(annotations=WRITE)
    async def reexport(ctx: Context, job_id: str, item_id: str, overrides: dict[str, Any],
                       build_run_id: str | None = None, idempotency_key: str | None = None,
                       project_id: str | None = None) -> dict[str, Any]:
        """Derive a new build from an existing one with export overrides (no regeneration). Defaults to the
        item's current build."""
        c, pid = await _ctx(ctx, project_id)
        return await run_gate(c, pid, job_id, gate="accept", item_ids=[item_id], route="reexport",
                              unit=lambda it: {**views.build_ref(it, build_run_id), "overrides": overrides},
                              key=idempotency_key)

    @mcp.tool(annotations=WRITE)
    async def retry_preview(ctx: Context, job_id: str, item_ids: list[str] | None = None,
                            idempotency_key: str | None = None, project_id: str | None = None) -> dict[str, Any]:
        """Retry the preview render of a valid build whose preview failed."""
        c, pid = await _ctx(ctx, project_id)
        return await run_gate(c, pid, job_id, gate="retry_preview", item_ids=item_ids, route="retry-preview",
                              unit=views.item_ref, key=idempotency_key)

    @mcp.tool(annotations=WRITE)
    async def run_transform(ctx: Context, job_id: str, item_ids: list[str] | None = None,
                            idempotency_key: str | None = None, project_id: str | None = None) -> dict[str, Any]:
        """Direct variant Jobs only (no prompt/candidates): run the confirmed deterministic transform."""
        c, pid = await _ctx(ctx, project_id)
        return await run_gate(c, pid, job_id, gate="run_transform", item_ids=item_ids, route="run-transform",
                              unit=views.item_ref, key=idempotency_key)

    @mcp.tool(annotations=WRITE)
    async def accept_build(ctx: Context, job_id: str, item_ids: list[str] | None = None, accept: bool = True,
                           idempotency_key: str | None = None, project_id: str | None = None) -> dict[str, Any]:
        """Gate 4: accept the current valid build as the final result (accept=false withdraws acceptance).
        Only structurally valid builds can be accepted; inspect items[].build first."""
        c, pid = await _ctx(ctx, project_id)
        return await run_gate(c, pid, job_id, gate="accept", item_ids=item_ids, route="accept-builds",
                              unit=lambda it: {**views.build_ref(it), "accept": accept}, key=idempotency_key)

    @mcp.tool(annotations=WRITE)
    async def publish(ctx: Context, job_id: str, item_ids: list[str] | None = None, make_current: bool = True,
                      idempotency_key: str | None = None, project_id: str | None = None) -> dict[str, Any]:
        """Gate 5: publish accepted items to the library (asynchronous; new asset or new version). Follow with
        wait_for_job(until='published'). Default items: all accepted ones."""
        c, pid = await _ctx(ctx, project_id)
        await fetch_job(c, pid, job_id)
        rows = (await c.get(f"{base(pid)}/jobs/{job_id}/publish-preview"))["items"]
        rows = [r for r in rows if not r["published"] and (not item_ids or r["item_id"] in item_ids)]
        if not rows:
            raise ToolError(f"no items are ready for 'publish' in job {job_id}: accept a build first")
        units = [{"item_id": r["item_id"], "build_run_id": r["build_run_id"],
                  "expected_item_revision": r["expected_item_revision"], "make_current": make_current}
                 for r in rows]
        res = await c.post(f"{base(pid)}/jobs/{job_id}:publish",
                           {"items": units, "idempotency_key": idem(idempotency_key)})
        return {"results": res, "job": views.job_summary(await fetch_job(c, pid, job_id))}


BINDING_FIELDS = ("item_id", "expected_item_revision", "candidate_set_id", "candidate_id", "image_sha256",
                  "prompt_revision_id", "qa_evaluation_id")


async def approve_proposals(c: StudioClient, pid: str, job_id: str, item_ids: list[str] | None,
                            key: str | None) -> dict[str, Any]:
    """preview-best -> approve exactly what it proposes (proposals carry the full binding)."""
    prefix = f"{base(pid)}/jobs/{job_id}"
    preview = await c.post(f"{prefix}:preview-best", {"item_ids": item_ids})
    proposals = preview["proposals"]
    if not proposals:
        raise ToolError(f"no items are ready for 'approve_best': {preview['skipped']}")
    units = [{k: p[k] for k in BINDING_FIELDS} for p in proposals]
    res = await c.post(f"{prefix}:approve-candidates", {"items": units, "idempotency_key": idem(key)})
    check_results(res["results"])
    return {"proposals": proposals, "skipped": preview["skipped"], "results": res["results"],
            "job": views.job_summary(await fetch_job(c, pid, job_id))}
