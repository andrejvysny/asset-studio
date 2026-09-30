"""Helpers shared by the production tool modules (jobs, gates, batches, variants)."""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp.exceptions import ToolError

from ..client import StudioClient


def base(pid: str) -> str:
    return f"/api/v2/projects/{pid}"


async def fetch_job(c: StudioClient, pid: str, job_id: str) -> dict[str, Any]:
    return await c.get(f"{base(pid)}/jobs/{job_id}")


def unwrap(body: Any) -> Any:
    """Sync gates answer {results: [...]}; async ones answer an operation body (kept whole)."""
    return body.get("results", body) if isinstance(body, dict) else body


def check_results(results: Any) -> None:
    """Per-unit failures are results, not HTTP errors. When every unit failed the call changed nothing: raise."""
    if not isinstance(results, list) or not results or not all(isinstance(r, dict) and "ok" in r for r in results):
        return
    failed = [r for r in results if not r["ok"]]
    if len(failed) == len(results):
        first = failed[0]
        more = f" (+{len(failed) - 1} more)" if len(failed) > 1 else ""
        raise ToolError(f"rejected {first.get('code', 'failed')}: {first.get('message', '')} "
                        f"[item {first.get('item_id')}]{more}\ndetail: {failed}")
