"""Which candidate approval a build attempt was made from, and whether it still matches the item's approval.

A build's own `inputs.approval_id` is the truth: the item's approval may since have moved to another candidate."""
from __future__ import annotations

from assetstudio_core.domain import BuildRun, JobItem, ReviewDecision
from assetstudio_storage.project import ProjectStore
from assetstudio_storage.repo import NotFound

from .records import load_build, load_decision


def candidate_identity(decision: ReviewDecision) -> tuple[str, str, str] | None:
    """(candidate_set_id, candidate_id, image_sha256) the decision approved; None for direct-transform decisions."""
    b = decision.bound
    if not b.get("candidate_set_id"):
        return None
    return b["candidate_set_id"], b.get("candidate_id", ""), b.get("image_sha256", "")


def run_decision(store: ProjectStore, job_id: str, run: BuildRun) -> ReviewDecision | None:
    aid = run.inputs.get("approval_id")
    if not aid:
        return None
    try:
        return load_decision(store, job_id, aid)
    except NotFound:
        return None


def build_matches_approval(store: ProjectStore, job_id: str, item: JobItem, run: BuildRun) -> bool:
    """True when the attempt was built from the item's current approval or from a new decision of the same
    candidate (same set, candidate and image bytes)."""
    if item.approval is None:
        return False
    aid = run.inputs.get("approval_id")
    if aid is None:
        return False
    if aid == item.approval:
        return True
    built = run_decision(store, job_id, run)
    try:
        current = load_decision(store, job_id, item.approval)
    except NotFound:
        return False
    if built is None:
        return False
    ident = candidate_identity(built)
    return ident is not None and ident == candidate_identity(current)


def restorable_build(store: ProjectStore, job_id: str, item: JobItem, identity: tuple[str, str, str]) -> str | None:
    """The most recent attempt built from this candidate: a deliberate return to it restores its attempts."""
    for rid in reversed(item.build_runs):
        run = load_build(store, job_id, rid)[0]
        d = run_decision(store, job_id, run)
        if d is not None and candidate_identity(d) == identity:
            return rid
    return None
