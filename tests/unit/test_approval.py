from pathlib import Path

import pytest
from jobcore.approval import OverrideRequired, approve, write_candidate_set
from jobcore.job_io import Job, JobConflict, JobError, sha256_file

REVIEW_STATES = ["prompt_enhanced", "prompt_confirmed", "candidates_generated", "qa_completed", "waiting_for_selection"]


def reviewed_job(root: Path, prompt: str = "barrel") -> Job:
    job = Job.create(root, {"prompt": prompt})
    for i in range(4):
        p = job.path(f"candidates/{i:02d}.png")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(f"image-{prompt}-{i}".encode())
    job.write_text("enhanced-prompt.final.txt", "effective prompt")
    for i in range(4):
        job.write_json(f"qa/{i:02d}.json", {"status": "recommended", "coverage": {"ran": 14, "total": 14, "missing": []}})
    write_candidate_set(job)
    for s in REVIEW_STATES:
        job.set_state(s)
    return job


def sha(job: Job, i: int) -> str:
    return sha256_file(job.path(f"candidates/{i:02d}.png"))


def test_approve_creates_attempt(tmp_path: Path) -> None:
    job = reviewed_job(tmp_path)
    attempt, created = approve(job, "cs-1", 2, sha(job, 2))
    assert created and attempt["id"] == "att-01" and attempt["index"] == 2
    assert job.state == "candidate_selected"
    assert job.path("model/attempts/att-01/selected.png").read_bytes() == job.path("candidates/02.png").read_bytes()
    assert job.read_json("job_state.json")["current_attempt"] == "att-01"


def test_repeat_approval_is_idempotent(tmp_path: Path) -> None:
    job = reviewed_job(tmp_path)
    a1, _ = approve(job, "cs-1", 1, sha(job, 1))
    a2, created = approve(job, "cs-1", 1, sha(job, 1))
    assert not created and a2["id"] == a1["id"] and len(job.list_attempts()) == 1


def test_sha_mismatch_rejected_without_side_effects(tmp_path: Path) -> None:
    job = reviewed_job(tmp_path)
    with pytest.raises(JobConflict):
        approve(job, "cs-1", 1, "0" * 64)
    assert job.state == "waiting_for_selection" and job.list_attempts() == []


def test_changed_image_invalidates_old_approval(tmp_path: Path) -> None:
    job = reviewed_job(tmp_path)
    old_sha = sha(job, 0)
    job.path("candidates/00.png").write_bytes(b"tampered")
    with pytest.raises(JobConflict):
        approve(job, "cs-1", 0, old_sha)


def test_stale_set_rejected(tmp_path: Path) -> None:
    job = reviewed_job(tmp_path)
    with pytest.raises(JobConflict):
        approve(job, "cs-0", 0, sha(job, 0))


def test_approval_bound_to_reviewed_job_not_newer(tmp_path: Path) -> None:
    job_a = reviewed_job(tmp_path, "barrel")
    sha_a = sha(job_a, 0)
    job_b = reviewed_job(tmp_path, "crate")  # created after A was reviewed
    approve(job_a, "cs-1", 0, sha_a)
    assert job_a.state == "candidate_selected" and job_b.state == "waiting_for_selection"
    with pytest.raises(JobConflict):  # A's sha can't approve B's candidate
        approve(job_b, "cs-1", 0, sha_a)


def test_busy_job_rejects_approval(tmp_path: Path) -> None:
    job = reviewed_job(tmp_path)
    with job.operation("cutout", "waiting_for_selection"):
        with pytest.raises(JobConflict):
            approve(job, "cs-1", 0, sha(job, 0))


@pytest.mark.parametrize("bad", [True, -1, "1", 1.0, None])
def test_bad_index(tmp_path: Path, bad: object) -> None:
    job = reviewed_job(tmp_path)
    with pytest.raises(JobError):
        approve(job, "cs-1", bad, sha(job, 0))  # type: ignore[arg-type]


def test_new_approval_after_failure_creates_new_attempt(tmp_path: Path) -> None:
    job = reviewed_job(tmp_path)
    a1, _ = approve(job, "cs-1", 0, sha(job, 0))
    job.update_attempt(a1["id"], state="failed_trellis")
    job.set_state("failed_cutout")
    a2, created = approve(job, "cs-1", 0, sha(job, 0))
    assert created and a2["id"] == "att-02"
    assert job.read_attempt("att-01")["state"] == "failed_trellis"  # old attempt untouched


@pytest.mark.parametrize("status", ["not_recommended", "unverified", None])
def test_non_recommended_needs_explicit_override(tmp_path: Path, status: str | None) -> None:
    job = reviewed_job(tmp_path)
    if status is None:
        job.path("qa/03.json").unlink()
    else:
        job.write_json("qa/03.json", {"status": status})
    with pytest.raises(OverrideRequired):
        approve(job, "cs-1", 3, sha(job, 3))
    assert job.list_attempts() == [] and job.state == "waiting_for_selection"
    attempt, created = approve(job, "cs-1", 3, sha(job, 3), override=True)
    assert created and attempt["qa_override"] is True and attempt["qa_status"] == status
    assert job.read_json("manifest.json")["selection"]["qa_override"] is True


def test_all_failed_can_still_be_approved(tmp_path: Path) -> None:
    job = reviewed_job(tmp_path)
    for i in range(4):
        job.write_json(f"qa/{i:02d}.json", {"status": "not_recommended"})
    attempt, _ = approve(job, "cs-1", 2, sha(job, 2), override=True)
    assert attempt["index"] == 2 and attempt["qa_override"]


def test_recommended_needs_no_override(tmp_path: Path) -> None:
    job = reviewed_job(tmp_path)
    attempt, _ = approve(job, "cs-1", 0, sha(job, 0))
    assert attempt["qa_override"] is False
