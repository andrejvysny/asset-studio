import os
from pathlib import Path

import pytest
from jobcore.job_io import Job, JobConflict, JobError, can_transition


def test_operation_validates_state_before_side_effects(tmp_path: Path) -> None:
    job = Job.create(tmp_path, {"prompt": "x"})
    ran = []
    with pytest.raises(JobConflict):
        with job.operation("generate", "prompt_confirmed"):
            ran.append(1)
    assert ran == [] and job.active_operation is None


def test_operation_is_exclusive_and_released(tmp_path: Path) -> None:
    job = Job.create(tmp_path, {"prompt": "x"})
    with job.operation("enhance", "created"):
        with pytest.raises(JobConflict):
            with job.operation("enhance", "created"):
                pass
    assert job.active_operation is None


def test_operation_released_on_error(tmp_path: Path) -> None:
    job = Job.create(tmp_path, {"prompt": "x"})
    with pytest.raises(RuntimeError):
        with job.operation("enhance", "created"):
            raise RuntimeError("boom")
    assert job.active_operation is None


def test_clear_stale_operation(tmp_path: Path) -> None:
    job = Job.create(tmp_path, {"prompt": "x"})
    state = job.read_json("job_state.json")
    state["active_operation"] = {"id": "x", "name": "trellis", "started_at": "t"}
    job.write_json("job_state.json", state)
    assert job.clear_stale_operation("restart") and job.active_operation is None


def test_attempt_numbering_is_monotonic(tmp_path: Path) -> None:
    job = Job.create(tmp_path, {"prompt": "x"})
    ids = [job.new_attempt({})["id"] for _ in range(3)]
    assert ids == ["att-01", "att-02", "att-03"]
    job.path("model/attempts/att-02/attempt.json").unlink()  # even a missing one is never reused
    assert job.new_attempt({})["id"] == "att-04"


def test_symlinked_job_dir_rejected(tmp_path: Path) -> None:
    out, elsewhere = tmp_path / "out", tmp_path / "elsewhere"
    out.mkdir()
    elsewhere.mkdir()
    os.symlink(elsewhere, out / "evil-job")
    with pytest.raises(JobError):
        Job(out, "evil-job")


def test_unknown_target_state_is_false_not_error() -> None:
    assert can_transition("failed_trellis", "bogus") is False
    assert can_transition("created", "bogus") is False


def test_nested_lock_does_not_deadlock(tmp_path: Path) -> None:
    job = Job.create(tmp_path, {"prompt": "x"})
    with job.locked(), job.locked():
        job.set_state("prompt_enhanced")
    assert job.state == "prompt_enhanced"
