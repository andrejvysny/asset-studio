"""Candidate approval binding and bulk "best recommended" proposals. Pure; storage-agnostic."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .domain import CandidateSet, JobItem, QaEvaluation
from .qa import recommendation_rank


class ReviewError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.detail = detail or {}


@dataclass(frozen=True)
class ApprovalRequest:
    item_id: str
    candidate_set_id: str
    candidate_id: str
    image_sha256: str
    prompt_revision_id: str
    qa_evaluation_id: str | None
    expected_item_revision: int
    override_qa: bool = False
    override_reason: str | None = None


def check_binding(item: JobItem, cset: CandidateSet | None, qa: QaEvaluation | None,
                  req: ApprovalRequest, blob_sha: str | None) -> dict[str, Any]:
    """Validates that the request names exactly the current reviewable bytes. Returns the decision payload."""
    if item.revision != req.expected_item_revision:
        raise ReviewError("stale_item", f"item changed (revision {item.revision}); reload the review")
    if item.cancelled:
        raise ReviewError("item_cancelled", "item is cancelled")
    if item.accepted_build is not None or (item.current_build and item.tasks.get("build")
                                           and item.tasks["build"].state in ("queued", "running")):
        raise ReviewError("build_in_progress", "a build is running or accepted for this item")
    if cset is None or item.current_set != req.candidate_set_id or cset.id != req.candidate_set_id:
        raise ReviewError("stale_set", f"candidate set {req.candidate_set_id} is not current ({item.current_set})")
    if cset.prompt_revision_id != req.prompt_revision_id:
        raise ReviewError("stale_prompt", "prompt revision does not match the candidate set")
    cand = next((c for c in cset.candidates if c.id == req.candidate_id), None)
    if cand is None:
        raise ReviewError("unknown_candidate", f"candidate {req.candidate_id} is not in set {cset.id}", 404)
    if cand.sha256 != req.image_sha256 or blob_sha != req.image_sha256:
        raise ReviewError("sha_mismatch", "image bytes changed; reload the review")
    current_qa = item.qa.get(cand.id)
    if req.qa_evaluation_id != current_qa:
        raise ReviewError("stale_qa", "QA evaluation changed; reload the review")
    status = qa.policy["status"] if qa else None
    if status != "recommended" and not req.override_qa:
        raise ReviewError("override_required", f"candidate is {status or 'unchecked'}; approve anyway to override",
                          409, {"qa_status": status})
    return {
        "bound": {"candidate_set_id": cset.id, "candidate_id": cand.id, "image_sha256": cand.sha256,
                  "artifact_id": cand.artifact_id, "prompt_revision_id": cset.prompt_revision_id,
                  "qa_evaluation_id": req.qa_evaluation_id, "seed": cand.seed},
        "qa_status": status,
        "override_qa": status != "recommended",
        "override_reason": req.override_reason if status != "recommended" else None,
        "failed_checks": (qa.policy["failed_major"] + qa.policy["failed_minor"]) if qa else [],
        "missing_checks": qa.policy["unavailable"] if qa else [],
    }


def propose_best(item: JobItem, cset: CandidateSet | None, qas: dict[str, QaEvaluation]) -> dict[str, Any]:
    """One proposal per undecided row: best recommended candidate, or a skip reason. Never approves."""
    if item.approval is not None and not item.regen_requested:
        return {"item_id": item.id, "skip": "already approved"}
    if item.regen_requested:
        return {"item_id": item.id, "skip": "marked for regeneration"}
    if cset is None:
        return {"item_id": item.id, "skip": "no candidates"}
    if any(c.id not in item.qa for c in cset.candidates):
        return {"item_id": item.id, "skip": "QA incomplete"}
    ranked = []
    for c in cset.candidates:
        qa = qas.get(item.qa[c.id])
        if qa is not None and qa.policy["status"] == "recommended":
            ranked.append((recommendation_rank(qa.policy), c.index, c, qa))
    if not ranked:
        return {"item_id": item.id, "skip": "no recommended candidate"}
    _, _, cand, qa = min(ranked, key=lambda r: (r[0], r[1]))
    return {"item_id": item.id, "candidate_set_id": cset.id, "candidate_id": cand.id, "image_sha256": cand.sha256,
            "prompt_revision_id": cset.prompt_revision_id, "qa_evaluation_id": qa.id,
            "expected_item_revision": item.revision, "index": cand.index}
