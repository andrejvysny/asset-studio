"""Per-item lifecycle derivation. A batch view is only ever an aggregate of item records."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .domain import BuildRun, JobItem, TaskRef

StageName = Literal["prompts", "candidates", "approve", "build", "publish", "done", "cancelled"]
TAB_ORDER: tuple[StageName, ...] = ("prompts", "candidates", "approve", "build", "publish")
READY_TO_TRANSFORM = "ready to transform"
ACTIVE = ("queued", "running", "cancel_requested", "reconciling")


@dataclass(frozen=True)
class ItemStage:
    stage: StageName
    state: str  # short machine label
    waiting_on_user: bool
    busy: bool
    failed: bool = False


def _task(tasks: dict[str, TaskRef], name: str) -> str | None:
    t = tasks.get(name)
    return t.state if t else None


def item_stage(item: JobItem, build: BuildRun | None, tasks: dict[str, TaskRef] | None = None,
               direct: bool = False) -> ItemStage:
    """`tasks`: per-family task view from the journal (legacy items fall back to their recorded refs).
    `direct`: deterministic transform Job: no prompt/candidate stages, the human gate is "run transform"."""
    tasks = item.tasks if tasks is None else tasks
    if item.cancelled:
        return ItemStage("cancelled", "cancelled", False, False)
    if item.published is not None and item.accepted_build is None:
        return ItemStage("done", "published", False, False)
    if item.accepted_build is not None:
        pub = _task(tasks, "publish")
        if pub in ACTIVE:
            return ItemStage("publish", "publishing", False, True)
        if item.published is not None:
            return ItemStage("done", "published", False, False)
        return ItemStage("publish", "ready to publish", True, False, failed=pub == "failed")
    if item.current_build is not None and build is not None:
        if build.status in ACTIVE or _task(tasks, "build") in ACTIVE:
            return ItemStage("build", "building", False, True)
        if build.status == "blocked":
            return ItemStage("build", "build blocked", True, False, failed=True)
        if build.status == "succeeded" and build.result == "valid":
            return ItemStage("build", "ready for final review", True, False)
        return ItemStage("build", f"build {build.status}", True, False, failed=True)
    if direct and item.approval is None:
        return ItemStage("build", READY_TO_TRANSFORM, True, False)
    if item.approval is not None and not item.regen_requested:
        return ItemStage("build", "approved", True, False)
    gen = _task(tasks, "generate")
    if item.prompt_confirmed is not None and item.prompt_confirmed == item.current_prompt and (
            item.current_set is None or gen in ACTIVE):
        if gen in ACTIVE:
            return ItemStage("candidates", "generating", False, True)
        return ItemStage("candidates", "generation failed" if gen == "failed" else "not generated", True, False,
                         failed=gen == "failed")
    if item.current_set is not None:
        qa = _task(tasks, "qa")
        if item.regen_requested:
            return ItemStage("approve", "marked for regeneration", True, False)
        gen_ref = tasks.get("generate")
        if qa not in ACTIVE and gen_ref is not None and gen_ref.progress.get("downstream_pending"):
            return ItemStage("approve", "QA queued", True, True)
        return ItemStage("approve", "QA running" if qa in ACTIVE else "undecided", True, qa in ACTIVE)
    enh = _task(tasks, "enhance")
    if enh in ACTIVE:
        return ItemStage("prompts", "enhancing", False, True)
    if item.current_prompt is None:
        return ItemStage("prompts", "enhance failed" if enh == "failed" else "brief only", True, False,
                         failed=enh == "failed")
    return ItemStage("prompts", "edited" if item.prompt_confirmed is None else "confirmed", True, False)


def aggregate(items: list[JobItem], builds: dict[str, BuildRun],
              tasks: dict[str, dict[str, TaskRef]] | None = None, direct: bool = False) -> dict:
    stages = {i.id: item_stage(i, builds.get(i.current_build or ""), (tasks or {}).get(i.id), direct)
              for i in items}
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
        "to_transform": sum(1 for i in live if stages[i.id].state == READY_TO_TRANSFORM),
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
    if c.get("to_transform"):
        return "run transform"
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


_STAGE_INDEX = {"prompts": 0, "candidates": 1, "approve": 2, "build": 3, "publish": 4, "done": 5, "cancelled": 5}
_TAB = {"prompts": 0, "candidates": 0, "approve": 0, "build": 1, "publish": 2, "done": 2, "cancelled": 0}


def _progress_label(s: dict, rounds: int, build_label: str) -> tuple[str, str]:
    """(label, state) in the design's words. state: wait | run | bad | draft | done."""
    st = s["state"]
    table = {
        "published": ("Published", "done"), "cancelled": ("Cancelled", "done"),
        "publishing": ("Publishing…", "run"), "ready to publish": ("Publish", "wait"),
        "building": (f"Building {build_label}…", "run"), "ready for final review": (f"Accept {build_label}", "wait"),
        "approved": (f"Build {build_label}", "wait"), "ready to transform": ("Run transform", "wait"),
        "generating": (f"Generating R{rounds + 1}…", "run"), "not generated": ("Generate candidates", "wait"),
        "generation failed": ("Retry generation", "bad"), "QA running": ("QA running…", "run"),
        "QA queued": ("QA queued", "wait"),
        "undecided": ("Approve a candidate", "wait"), "marked for regeneration": ("Regenerate", "wait"),
        "enhancing": ("Enhancing prompt…", "run"), "brief only": ("Draft · not run", "draft"),
        "enhance failed": ("Retry enhance", "bad"), "edited": ("Confirm prompt", "wait"),
        "confirmed": ("Confirm prompt", "wait"),
    }
    if st in table:
        return table[st]
    if s.get("failed"):
        return (f"Retry {build_label}", "bad")
    return (st, "wait" if s.get("waiting_on_user") else "run" if s.get("busy") else "draft")


def progress(agg: dict, rounds: int, build_label: str) -> dict:
    """Design pills: 5 stages (prompt, cands, approve, build, publish) + the next action of the least advanced
    live item. A one-item Job (design: one Job = one asset) shows exactly its item."""
    stages = [s for s in agg["item_stages"].values() if s["stage"] != "cancelled"] or list(
        agg["item_stages"].values())
    if not stages:
        return {"stage": 0, "state": "draft", "label": "Empty", "tab": 0}
    s = min(stages, key=lambda x: _STAGE_INDEX[x["stage"]])
    label, state = _progress_label(s, rounds, build_label)
    return {"stage": _STAGE_INDEX[s["stage"]], "state": state, "label": label, "tab": _TAB[s["stage"]]}
