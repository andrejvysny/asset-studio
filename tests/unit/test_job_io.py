from pathlib import Path

import job_io
import pytest
from job_io import Job, JobError


def make(tmp_path: Path) -> Job:
    return Job.create(tmp_path, {"prompt": "Wooden Medieval Barrel!"})


def test_create_layout(tmp_path: Path) -> None:
    job = make(tmp_path)
    assert job_io.JOB_ID_RE.fullmatch(job.id)
    assert "wooden-medieval-barrel" in job.id
    for f in ["request.json", "prompt.txt", "job_state.json", "manifest.json", "run.log"]:
        assert (job.root / f).is_file()
    assert job.state == "created"


def test_no_overwrite(tmp_path: Path) -> None:
    job = make(tmp_path)
    with pytest.raises(FileExistsError):
        Job.create(tmp_path, {"prompt": "x"}, job_id=job.id)


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "A", ".hidden", "x" * 81, "a b"])
def test_invalid_ids(tmp_path: Path, bad: str) -> None:
    with pytest.raises(JobError):
        Job(tmp_path, bad)


@pytest.mark.parametrize("rel", ["../escape.txt", "/etc/passwd", "candidates/../../x"])
def test_path_traversal(tmp_path: Path, rel: str) -> None:
    with pytest.raises(JobError):
        make(tmp_path).path(rel)


def test_happy_path_transitions(tmp_path: Path) -> None:
    job = make(tmp_path)
    for s in job_io.STATES[1:]:
        job.set_state(s)
    assert job.state == "completed"
    assert len(job.read_json("job_state.json")["history"]) == len(job_io.STATES)


def test_illegal_skip(tmp_path: Path) -> None:
    with pytest.raises(JobError):
        make(tmp_path).set_state("candidates_generated")


def test_failure_and_retry(tmp_path: Path) -> None:
    job = make(tmp_path)
    for s in job_io.STATES[1:8]:  # .. cutout_completed
        job.set_state(s)
    job.set_state("failed_trellis", error="OOM")
    assert job.read_json("job_state.json")["history"][-1]["error"] == "OOM"
    job.set_state("model_generated")  # retry succeeded


def test_reselect_after_completion(tmp_path: Path) -> None:
    job = make(tmp_path)
    for s in job_io.STATES[1:]:
        job.set_state(s)
    job.set_state("candidate_selected")


def test_manifest_merge(tmp_path: Path) -> None:
    job = make(tmp_path)
    job.update_manifest(models={"a": 1})
    job.update_manifest(models={"b": 2}, seeds=[1, 2])
    m = job.read_json("manifest.json")
    assert m["models"] == {"a": 1, "b": 2} and m["seeds"] == [1, 2]
