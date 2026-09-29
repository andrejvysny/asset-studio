"""Per-item lifecycle derivation. A batch view is only ever an aggregate of item records."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .domain import BuildRun, JobItem

StageName = Literal["prompts", "candidates", "approve", "build", "publish", "done", "cancelled"]
TAB_ORDER: tuple[StageName, ...] = ("prompts", "candidates", "approve", "build", "publish")
ACTIVE = ("queued", "running", "cancel_requested", "reconciling")


@dataclass(frozen=True)
class ItemStage:
    stage: StageName
    state: str  # short machine label
    waiting_on_user: bool
    busy: bool
    failed: bool = False


def _task(item: JobItem, name: str) -> str | None:
    t = item.tasks.get(name)
    return t.state if t else None


def item_stage(item: JobItem, build: BuildRun | None) -> ItemStage:
    if item.cancelled:
        return ItemStage("cancelled", "cancelled", False, False)
    if item.published is not None and item.accepted_build is None:
        return ItemStage("done", "published", False, False)
    if item.accepted_build is not None:
        pub = _task(item, "publish")
        if pub in ACTIVE:
            return ItemStage("publish", "publishing", False, True)
        if item.published is not None:
            return ItemStage("done", "published", False, False)
        return ItemStage("publish", "ready to publish", True, False, failed=pub == "failed")
    if item.current_build is not None and build is not None:
        if build.status in ACTIVE:
            return ItemStage("build", build.status, False, True)
        if build.status == "succeeded" and build.result == "valid":
            return ItemStage("build", "ready for final review", True, False)
        return ItemStage("build", f"build {build.status}", True, False, failed=True)
    if item.approval is not None and not item.regen_requested:
        return ItemStage("build", "approved", True, False)
    gen = _task(item, "generate")
    if item.prompt_confirmed is not None and item.prompt_confirmed == item.current_prompt and (
            item.current_set is None or gen in ACTIVE):
        if gen in ACTIVE:
            return ItemStage("candidates", "generating", False, True)
        return ItemStage("candidates", "generation failed" if gen == "failed" else "not generated", True, False,
                         failed=gen == "failed")
    if item.current_set is not None:
        qa = _task(item, "qa")
        if item.regen_requested:
            return ItemStage("approve", "marked for regeneration", True, False)
        return ItemStage("approve", "QA running" if qa in ACTIVE else "undecided", True, qa in ACTIVE)
    enh = _task(item, "enhance")
    if enh in ACTIVE:
        return ItemStage("prompts", "enhancing", False, True)
    if item.current_prompt is None:
        return ItemStage("prompts", "enhance failed" if enh == "failed" else "brief only", True, False,
                         failed=enh == "failed")
    return ItemStage("prompts", "edited" if item.prompt_confirmed is None else "confirmed", True, False)


def aggregate(items: list[JobItem], builds: dict[str, BuildRun]) -> dict:
    stages = {i.id: item_stage(i, builds.get(i.current_build or "")) for i in items}
    live = [i for i in items if not i.cancelled]
    n = len(live)
    count = {
        "items": n,
        "prompts": sum(1 for i in live if i.current_prompt is not None),
        "confirmed": sum(1 for i in live if i.prompt_confirmed is not None),
        "candidates": sum(1 for i in live if i.current_set is not None),
        "approved": sum(1 for i in live if i.approval is not None and not i.regen_requested),
        "regenerate": sum(1 for i in live if i.regen_requested),
        "built": sum(1 for i in live if (b := builds.get(i.current_build or "")) and b.result == "valid"),
        "accepted": sum(1 for i in live if i.accepted_build is not None),
        "published": sum(1 for i in live if i.published is not None),
        "busy": sum(1 for s in stages.values() if s.busy),
        "failed": sum(1 for s in stages.values() if s.failed),
        "cancelled": len(items) - n,
    }
    by_stage = {t: sum(1 for i in live if stages[i.id].stage == t) for t in (*TAB_ORDER, "done")}
    waiting = [s for i, s in stages.items() if s.waiting_on_user and not s.failed]
    first = next((t for t in TAB_ORDER if by_stage[t]), "done")
    return {"counts": count, "by_stage": by_stage, "current_tab": first,
            "waiting_on_user": bool(waiting), "waiting_items": len(waiting),
            "item_stages": {k: v.__dict__ for k, v in stages.items()}}


def next_action(agg: dict) -> str:
    c, s = agg["counts"], agg["by_stage"]
    if c["items"] == 0:
        return "empty"
    if c["busy"]:
        return "running…"
    if s["prompts"]:
        return f"review {s['prompts']} prompts"
    if s["candidates"]:
        return f"generate {s['candidates']}"
    if s["approve"]:
        return f"approve {s['approve']}"
    if s["build"]:
        return f"build / review {s['build']}"
    if s["publish"]:
        return f"publish {s['publish']}"
    return "done"
