"""Item read models: what the Job screens render (legal actions, candidate rounds, references, approval)."""
from __future__ import annotations

from typing import Any

from assetstudio_core.domain import BuildRun, CandidateSet, Job, JobItem
from assetstudio_core.ids import derived_id
from assetstudio_core.lifecycle import item_stage

from ..registry import ProjectContext
from ..studio import Studio
from . import build_modes
from .build_binding import build_matches_approval, candidate_identity, run_decision
from .records import load_build, load_cset, load_decision, load_prompt, load_qa
from .taskview import as_json, busy, item_tasks


def legal_actions(item: JobItem, tasks: dict[str, Any], stage_busy: bool, build: BuildRun | None,
                  build_available: bool, direct: bool = False, matches: bool = True) -> dict[str, bool]:
    """`matches`: the current build was made from the item's current approval (or the same candidate)."""
    gen_busy = busy(tasks, "generate")
    build_active = busy(tasks, "build")
    if direct:  # no prompt, candidates or approval: one confirmed deterministic transform
        rerunnable = build is None or build.status in ("failed", "blocked", "cancelled")
        no = dict.fromkeys(("edit_prompt", "enhance", "confirm", "regenerate", "approve", "mark_regenerate",
                            "build"), False)
        return {**no, "run_transform": rerunnable and not build_active and item.accepted_build is None
                and not stage_busy,
                "accept": build is not None and build.result == "valid" and item.accepted_build is None
                and matches,
                "publish": item.accepted_build is not None and not stage_busy,
                "retry_preview": build is not None and build.result == "valid" and build.preview == "failed"
                and not build_active}
    # Rounds: every candidate set is kept, so prompts stay editable (a confirmed new prompt is the next round)
    # until a build is accepted; only running enhancement/generation blocks them.
    can_prompt = item.accepted_build is None and not busy(tasks, "generate", "enhance")
    return {
        "run_transform": False,
        "edit_prompt": can_prompt and item.current_prompt is not None,
        "enhance": can_prompt,
        "confirm": can_prompt and item.current_prompt is not None,
        "regenerate": can_prompt and item.current_set is not None and not build_active,
        "approve": item.current_set is not None and not build_active and item.accepted_build is None,
        "mark_regenerate": item.current_set is not None and not build_active and item.accepted_build is None
        and not gen_busy,
        "build": build_available and item.approval is not None and not item.regen_requested and not build_active
        and item.accepted_build is None and (build is None or build.result != "valid" or not matches),
        "accept": build is not None and build.result == "valid" and item.accepted_build is None and matches,
        "publish": item.accepted_build is not None and not stage_busy,
        "retry_preview": build is not None and build.result == "valid" and build.preview == "failed"
        and not build_active,
    }


def _candidate_view(ctx: ProjectContext, job_id: str, cand: Any, qa_id: str | None) -> dict[str, Any]:
    qa = load_qa(ctx.store, job_id, qa_id) if qa_id else None
    return {**cand.model_dump(), "qa": None if qa is None else {
        "id": qa.id, "status": qa.policy["status"], "coverage": qa.policy["coverage"],
        "policy": qa.policy, "results": qa.results, "not_evaluated": qa.policy.get("not_evaluated")}}


def _round_view(ctx: ProjectContext, item: JobItem, cset: CandidateSet, generating: bool) -> dict[str, Any]:
    prompt = load_prompt(ctx.store, item.job_id, cset.prompt_revision_id)
    b = prompt.bindings
    qa_map = item.qa if cset.id == item.current_set else item.qa_history.get(cset.id, {})
    return {"number": cset.number, "candidate_set_id": cset.id, "prompt_revision_id": prompt.id,
            "prompt": {"positive": prompt.positive, "origin": prompt.origin, "preset": b.get("preset"),
                       "references_revision": b.get("references_revision"), "additions": b.get("additions", []),
                       "reference_count": len(b.get("reference_ids") or [])},
            "created_at": cset.created_at, "requested": cset.requested, "generating": generating,
            "candidates": [_candidate_view(ctx, item.job_id, c, qa_map.get(c.id)) for c in cset.candidates]}


def _in_flight(studio: Studio, tasks: dict[str, Any], item: JobItem, ctx: ProjectContext) -> dict[str, Any] | None:
    """The set being generated has no record yet: describe it from its task."""
    ref = tasks.get("generate")
    if ref is None or not busy(tasks, "generate"):
        return None
    task = studio.journal.tasks.get(ref.op_id)
    cs_id = derived_id("cs", ref.op_id)
    if task is None or cs_id in item.candidate_sets:
        return {"cs_id": cs_id} if task is not None else None
    return {"cs_id": cs_id, "prompt_revision_id": task.inputs.get("prompt_revision_id"),
            "progress": ref.progress}


def rounds_view(studio: Studio, ctx: ProjectContext, item: JobItem, tasks: dict[str, Any]) -> list[dict[str, Any]]:
    flight = _in_flight(studio, tasks, item, ctx)
    out = [_round_view(ctx, item, load_cset(ctx.store, item.job_id, cid), bool(flight) and flight["cs_id"] == cid)
           for cid in item.candidate_sets]
    out.sort(key=lambda r: r["number"])
    if flight and flight["cs_id"] not in item.candidate_sets:
        out.append({"number": len(item.candidate_sets) + 1, "candidate_set_id": None,
                    "prompt_revision_id": flight["prompt_revision_id"], "prompt": None, "created_at": None,
                    "requested": (flight["progress"] or {}).get("total"), "generating": True,
                    "progress": flight["progress"], "candidates": []})
    return out


def approved_view(ctx: ProjectContext, item: JobItem, rounds: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not item.approval:
        return None
    bound = load_decision(ctx.store, item.job_id, item.approval).bound
    set_id = bound.get("candidate_set_id")
    if set_id is None:
        return None
    rnd = next((r["number"] for r in rounds if r["candidate_set_id"] == set_id), None)
    return {"candidate_set_id": set_id, "candidate_id": bound.get("candidate_id"), "round": rnd}


def _attempt_source(ctx: ProjectContext, job_id: str, run: BuildRun) -> dict[str, Any] | None:
    """The candidate this attempt was actually built from (its own decision, not the item's current approval)."""
    d = run_decision(ctx.store, job_id, run)
    ident = candidate_identity(d) if d else None
    if ident is None:
        return None
    number = load_cset(ctx.store, job_id, ident[0]).number
    return {"candidate_set_id": ident[0], "candidate_id": ident[1], "round": number}


def prompt_stale(item: JobItem, prompt: Any) -> bool:
    rev = prompt.bindings.get("references_revision") if prompt else None
    return rev is not None and rev != item.references_revision


def _history(ctx: ProjectContext, job_id: str, item: JobItem) -> list[dict[str, Any]]:
    history = []
    for rid in item.build_runs:
        r = load_build(ctx.store, job_id, rid)[0]
        history.append({"approval_id": r.inputs.get("approval_id"), "source": _attempt_source(ctx, job_id, r),
                        "matches_approval": build_matches_approval(ctx.store, job_id, item, r),
                        "id": r.id, "status": r.status, "result": r.result, "kind": r.kind, "error": r.error,
                        "derived_from": r.derived_from, "created_at": r.created_at, "preview": r.preview,
                        "checkpoints": sorted(r.checkpoints), "has_raw": "raw" in r.artifacts,
                        "accepted": r.id == item.accepted_build, "current": r.id == item.current_build,
                        **build_modes.history_extra(r)})
    return history


def item_view(studio: Studio, ctx: ProjectContext, job: Job, item: JobItem, build_available: bool) -> dict[str, Any]:
    store = ctx.store
    build = load_build(store, job.id, item.current_build)[0] if item.current_build else None
    tasks = item_tasks(studio, ctx.id, item)
    st = item_stage(item, build, tasks, job.direct)
    prompt = load_prompt(store, job.id, item.current_prompt) if item.current_prompt else None
    cset = load_cset(store, job.id, item.current_set) if item.current_set else None
    candidates = [_candidate_view(ctx, job.id, c, item.qa.get(c.id)) for c in cset.candidates] if cset else []
    rounds = rounds_view(studio, ctx, item, tasks)
    approval = load_decision(store, job.id, item.approval) if item.approval else None
    history = _history(ctx, job.id, item)
    return {
        **item.model_dump(mode="json", exclude={"tasks"}),
        "batch_id": job.id,  # v1 compatibility: the old API called the Job a batch
        "tasks": as_json(tasks),
        "stage": st.__dict__,
        "prompt": prompt.model_dump(mode="json") if prompt else None,
        "prompt_stale": prompt_stale(item, prompt),
        "prompt_locked": item.current_set is not None and not item.regen_requested,
        "candidate_set": None if cset is None else {**cset.model_dump(mode="json", exclude={"candidates"}),
                                                    "candidates": candidates},
        "rounds": rounds,
        "approved": approved_view(ctx, item, rounds),
        "approval_detail": approval.model_dump(mode="json") if approval else None,
        "build": build.model_dump(mode="json") if build else None,
        "build_history": history,
        "legal": legal_actions(item, tasks, st.busy, build, build_available, job.direct,
                               build is None or build_matches_approval(store, job.id, item, build)),
    }
