"""Candidate sets and approvals bound to (job, set, index, image sha256). No ComfyUI imports."""
from __future__ import annotations

import shutil
from typing import Any

from .job_io import SELECTABLE, Job, JobConflict, JobError, now_iso, sha256_file

TERMINAL_FAILED = {"failed_cutout", "failed_trellis", "failed_postprocess"}


def write_candidate_set(job: Job) -> dict:
    """Freeze the generated candidates: approvals must reference these exact bytes."""
    images = {p.stem: sha256_file(p) for p in sorted(job.path("candidates").glob("*.png"))}
    if not images:
        raise JobError("no candidates to freeze")
    prev = job.read_json("candidates/set.json") if job.path("candidates/set.json").is_file() else None
    n = int(prev["set_id"].split("-")[1]) + 1 if prev else 1
    cset = {"set_id": f"cs-{n}", "created_at": now_iso(), "images": images}
    job.write_json("candidates/set.json", cset)
    return cset


def read_candidate_set(job: Job) -> dict:
    if not job.path("candidates/set.json").is_file():
        raise JobConflict("job has no frozen candidate set")
    return job.read_json("candidates/set.json")


def _current_attempt(job: Job) -> dict | None:
    current = job.read_json("job_state.json").get("current_attempt")
    return job.read_attempt(current) if current else None


class OverrideRequired(JobConflict):
    """Candidate is not QA-recommended; the user must explicitly approve anyway (QA stays advisory)."""


def approve(job: Job, set_id: str, index: int, image_sha256: str, override: bool = False) -> tuple[dict, bool]:
    """Returns (attempt, created). Identical repeat approval returns the existing attempt (idempotent).

    QA never blocks: a not_recommended / unverified / unchecked candidate is approvable with override=True,
    which is recorded on the attempt and in the manifest.
    """
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise JobError("index must be a non-negative integer")
    name = f"{index:02d}"
    with job.locked():
        state = job.read_json("job_state.json")
        if state.get("active_operation"):
            raise JobConflict(f"job busy: {state['active_operation']['name']}")
        job.require_state(SELECTABLE)
        cset = read_candidate_set(job)
        if cset["set_id"] != set_id:
            raise JobConflict(f"stale candidate set {set_id!r}; current is {cset['set_id']!r}")
        if name not in cset["images"]:
            raise JobError(f"candidate {name} not in set {set_id}")
        src = job.path(f"candidates/{name}.png")
        if cset["images"][name] != image_sha256 or sha256_file(src) != image_sha256:
            raise JobConflict(f"image sha256 mismatch for candidate {name}; reload the review")

        current = _current_attempt(job)
        if (current and current.get("candidate_set") == set_id and current.get("index") == index
                and current.get("state") not in TERMINAL_FAILED):
            return current, False

        qa = job.read_json(f"qa/{name}.json") if job.path(f"qa/{name}.json").is_file() else None
        qa_status = qa.get("status") if qa else None
        if qa_status != "recommended" and not override:
            raise OverrideRequired(f"candidate {name} is {qa_status or 'unchecked'}; confirm override to approve anyway")
        attempt = job.new_attempt({
            "candidate_set": set_id, "index": index, "candidate": f"{name}.png", "image_sha256": image_sha256,
            "qa_status": qa_status, "qa_coverage": qa.get("coverage") if qa else None,
            "qa_override": qa_status != "recommended",
        })
        adir = job.attempt_dir(attempt["id"])
        shutil.copy2(src, job.path(f"{adir}/selected.png"))
        if qa is not None:
            job.write_json(f"{adir}/selected_qa.json", qa)
        state = job.read_json("job_state.json")
        state["current_attempt"] = attempt["id"]
        job.write_json("job_state.json", state)
        job.set_state("candidate_selected")
        final_prompt = job.path("enhanced-prompt.final.txt")
        job.update_manifest(
            current_attempt=attempt["id"],
            selection={"candidate_set": set_id, "index": index, "candidate": f"{name}.png",
                       "image_sha256": image_sha256, "approved_at": attempt["created_at"],
                       "final_prompt": final_prompt.read_text() if final_prompt.is_file() else None,
                       "qa_status": attempt["qa_status"], "qa_coverage": attempt["qa_coverage"],
                       "qa_override": attempt["qa_override"]},
            # Outputs belong to attempts; never leave pointers to a previous attempt's files.
            outputs=None, mesh=None, trellis=None,
        )
    job.log(f"approved {name} (set {set_id}) -> {attempt['id']}"
            + (f" [QA override: {attempt['qa_status'] or 'unchecked'}]" if attempt["qa_override"] else ""))
    return attempt, True


def require_current_attempt(job: Job, attempt_id: str, allowed_states: set[str]) -> dict[str, Any]:
    current = job.read_json("job_state.json").get("current_attempt")
    if current != attempt_id:
        raise JobConflict(f"attempt {attempt_id} is not current ({current})")
    attempt = job.read_attempt(attempt_id)
    if attempt["state"] not in allowed_states:
        raise JobConflict(f"attempt {attempt_id} is {attempt['state']}, expected {sorted(allowed_states)}")
    return attempt
