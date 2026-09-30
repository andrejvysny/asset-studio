"""Agent-sized projections of REST views and gate binding resolution.

Binding: a gate tool names a Job and optionally items/ids. Omitted ids are taken from the item's current state
(current prompt, current candidate set, current approval, current build) together with its revision, so the Studio
still refuses anything that changed in between (409 stale_item). Explicit ids are passed through untouched.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError

Item = dict[str, Any]

# Default selection per gate when no item_ids are given: legal now AND not already past this gate.
DEFAULT_SELECT: dict[str, Callable[[Item], bool]] = {
    "enhance": lambda it: it["legal"].get("enhance", False) and it.get("current_prompt") is None,
    "confirm": lambda it: it["legal"].get("confirm", False) and it["stage"]["stage"] == "prompts",
    "approve": lambda it: it["legal"].get("approve", False) and it.get("approval") is None,
    "build": lambda it: it["legal"].get("build", False) and it.get("current_build") is None,
    "accept": lambda it: it["legal"].get("accept", False),
    "publish": lambda it: it["legal"].get("publish", False),
    "retry_preview": lambda it: it["legal"].get("retry_preview", False),
    "run_transform": lambda it: it["legal"].get("run_transform", False),
}


def select_items(job: dict[str, Any], gate: str, item_ids: list[str] | None) -> list[Item]:
    items = [it for it in job["items"] if not it.get("cancelled")]
    if item_ids:
        by_id = {it["id"]: it for it in items}
        missing = [i for i in item_ids if i not in by_id]
        if missing:
            raise ToolError(f"unknown or cancelled items in job {job['id']}: {', '.join(missing)}")
        return [by_id[i] for i in item_ids]
    chosen = [it for it in items if DEFAULT_SELECT[gate](it)]
    if not chosen:
        states = "; ".join(f"{it['id']} ({it['name']}): {it['stage']['stage']}/{it['stage']['state']}"
                           for it in items) or "no items"
        raise ToolError(f"no items are ready for '{gate}' in job {job['id']}: {states}")
    return chosen


def confirm_unit(it: Item, prompt_revision_id: str | None = None) -> dict[str, Any]:
    prompt = prompt_revision_id or it.get("current_prompt")
    if not prompt:
        raise ToolError(f"item {it['id']} has no prompt yet (enhance first or wait_for_job until='prompts')")
    return {"item_id": it["id"], "prompt_revision_id": prompt, "expected_item_revision": it["revision"]}


def _candidate_sets(it: Item) -> list[dict[str, Any]]:
    sets = [it["candidate_set"]] if it.get("candidate_set") else []
    return sets + [r for r in it.get("rounds", []) if r.get("candidate_set_id") != (sets[0]["id"] if sets else None)]


def approve_unit(it: Item, candidate_id: str, override_qa: bool = False,
                 override_reason: str | None = None) -> dict[str, Any]:
    """Finds the candidate in the current set or an earlier round and binds its exact hash, prompt and QA."""
    for s in _candidate_sets(it):
        for c in s.get("candidates", []):
            if c["id"] == candidate_id:
                return {"item_id": it["id"], "expected_item_revision": it["revision"],
                        "candidate_set_id": s.get("id") or s["candidate_set_id"], "candidate_id": c["id"],
                        "image_sha256": c["sha256"], "prompt_revision_id": s["prompt_revision_id"],
                        "qa_evaluation_id": (c.get("qa") or {}).get("id"), "override_qa": override_qa,
                        "override_reason": override_reason}
    raise ToolError(f"candidate {candidate_id} not found on item {it['id']}")


def build_unit(it: Item, mode: str = "build", overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    if not it.get("approval"):
        raise ToolError(f"item {it['id']} has no approved candidate")
    return {"item_id": it["id"], "approval_id": it["approval"], "expected_item_revision": it["revision"],
            "mode": mode, "overrides": overrides or {}}


def build_ref(it: Item, build_run_id: str | None = None) -> dict[str, Any]:
    run = build_run_id or it.get("current_build")
    if not run:
        raise ToolError(f"item {it['id']} has no build")
    return {"item_id": it["id"], "build_run_id": run, "expected_item_revision": it["revision"]}


def item_ref(it: Item) -> dict[str, Any]:
    return {"item_id": it["id"], "expected_item_revision": it["revision"]}


# --- compact views ---------------------------------------------------------------------------------------------
def _candidate(c: dict[str, Any]) -> dict[str, Any]:
    qa = c.get("qa") or {}
    failed = [f"{r.get('rule_id')} ({r.get('severity')})" for r in qa.get("results", []) if r.get("result") == "fail"]
    return {"id": c["id"], "index": c.get("index"), "artifact_id": c.get("artifact_id"), "sha256": c.get("sha256"),
            "qa_status": qa.get("status"), "qa_failed": failed or None}


def _build(b: dict[str, Any] | None) -> dict[str, Any] | None:
    if not b:
        return None
    checks = (b.get("validation") or {}).get("checks", [])
    return {"id": b.get("id"), "status": b.get("status"), "result": b.get("result"),
            "failed_checks": [c["id"] for c in checks if not c.get("ok")] or None,
            "artifacts": b.get("artifacts"), "error": b.get("error")}


def item_summary(it: Item) -> dict[str, Any]:
    prompt = it.get("prompt") or {}
    cset = it.get("candidate_set") or {}
    tasks = {k: {"state": v.get("state"), "error": v.get("error")} for k, v in (it.get("tasks") or {}).items()}
    return {
        "id": it["id"], "name": it["name"], "revision": it["revision"], "stage": it["stage"],
        "legal": sorted(k for k, v in it["legal"].items() if v),
        "prompt": {"id": prompt.get("id"), "description": prompt.get("description"),
                   "positive": prompt.get("positive"), "negative": prompt.get("negative"),
                   "confirmed": it.get("prompt_confirmed") == prompt.get("id")} if prompt else None,
        "candidate_set_id": cset.get("id"), "candidates": [_candidate(c) for c in cset.get("candidates", [])],
        "rounds": len(it.get("rounds", [])), "approval": it.get("approval"),
        "build": _build(it.get("build")), "accepted_build": it.get("accepted_build"),
        "published": it.get("published"), "tasks": tasks,
    }


JOB_KEYS = ("id", "alias", "title", "kind", "recipe_id", "category_id", "source", "counts", "next_action",
            "waiting_on_user", "active_run", "progress", "candidate_count", "config_revision", "batch", "variant")


def job_summary(job: dict[str, Any]) -> dict[str, Any]:
    return {**{k: job.get(k) for k in JOB_KEYS}, "items": [item_summary(it) for it in job.get("items", [])]}
