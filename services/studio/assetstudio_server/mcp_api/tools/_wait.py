"""wait_for_job predicates. `counts.busy` = items whose stage is running a task (enhancing, generating, QA,
building, publishing); a finished gate leaves busy == 0, so each target also checks the item fields it produces."""
from __future__ import annotations

from typing import Any

Item = dict[str, Any]
UNTIL = ("prompts", "candidates", "builds", "published", "idle")
BUILD_FINAL = ("succeeded", "failed", "blocked", "cancelled")
TASK_BAD = ("failed", "blocked")


def _live(job: dict[str, Any]) -> list[Item]:
    return [it for it in job["items"] if not it.get("cancelled")]


def _candidates(items: list[Item]) -> list[Item]:
    return [it for it in items if it.get("prompt_confirmed") is not None]


def _builds(items: list[Item]) -> list[Item]:
    return [it for it in items if it.get("approval") is not None and not it.get("regen_requested")]


_SCOPE = {"prompts": lambda its: its, "candidates": _candidates, "builds": _builds, "published": lambda its: its,
          "idle": lambda its: its}
_DONE = {
    "prompts": lambda it: it.get("current_prompt") is not None,
    "candidates": lambda it: it.get("current_set") is not None,
    "builds": lambda it: (it.get("build") or {}).get("status") in BUILD_FINAL,
    "published": lambda it: it.get("published") is not None,
    "idle": lambda it: True,
}


def evaluate(job: dict[str, Any], until: str) -> tuple[bool, str]:
    """(done, reason). Vacuous targets (nothing at that gate) are done with an explanatory reason."""
    if job["counts"]["busy"]:
        return False, f"{job['counts']['busy']} item(s) still running"
    scope = _SCOPE[until](_live(job))
    if until in ("candidates", "builds") and not scope:
        return True, f"nothing to wait for: no item has reached the {until} gate"
    waiting = [it["name"] for it in scope if not _DONE[until](it)]
    if waiting:
        return False, "not yet: " + ", ".join(waiting)
    return True, f"{until} reached"


def task_errors(job: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for it in _live(job):
        for family, t in (it.get("tasks") or {}).items():
            if t.get("state") in TASK_BAD:
                out.append({"item_id": it["id"], "name": it["name"], "task": family, "state": t["state"],
                            "error": t.get("error")})
        build = it.get("build") or {}
        if build.get("status") in ("failed", "blocked") and not any(e["item_id"] == it["id"] for e in out):
            out.append({"item_id": it["id"], "name": it["name"], "task": "build", "state": build["status"],
                        "error": build.get("error")})
    return out
