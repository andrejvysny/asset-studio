"""Slot -> (job, attempt) assignments in library/assignments.json. File-backed, atomic, locked."""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from jobcore.job_io import Job, JobConflict, JobError, atomic_write_bytes, now_iso


class AssignmentStore:
    def __init__(self, library_dir: Path, output_root: Path) -> None:
        self.path = library_dir / "assignments.json"
        self.lock_path = library_dir / ".assignments.lock"
        self.output_root = output_root
        library_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def all(self) -> dict[str, dict]:
        return json.loads(self.path.read_text()) if self.path.is_file() else {}

    def _write(self, data: dict) -> None:
        atomic_write_bytes(self.path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode())

    def assign(self, slot_id: str, job_id: str, attempt_id: str) -> dict:
        job = Job(self.output_root, job_id)
        attempt = job.read_attempt(attempt_id)  # validates attempt id + existence
        if attempt.get("state") != "completed" or not attempt.get("validated"):
            raise JobConflict(f"{job_id}/{attempt_id} is {attempt.get('state')} (validated={attempt.get('validated')}); "
                              "only completed, validated attempts can be assigned")
        with self._locked():
            data = self.all()
            # An attempt occupies at most one slot: reassigning moves it.
            for sid, a in list(data.items()):
                if a["job_id"] == job_id and a["attempt_id"] == attempt_id and sid != slot_id:
                    del data[sid]
            entry = {"job_id": job_id, "attempt_id": attempt_id, "assigned_at": now_iso(),
                     "glb": f"{job.attempt_dir(attempt_id)}/processed/model.glb",
                     "triangles": (attempt.get("mesh") or {}).get("triangles")}
            data[slot_id] = entry
            self._write(data)
        return entry

    def unassign(self, slot_id: str) -> bool:
        with self._locked():
            data = self.all()
            if slot_id not in data:
                return False
            del data[slot_id]
            self._write(data)
        return True

    def slot_of(self, job_id: str, attempt_id: str) -> str | None:
        return next((s for s, a in self.all().items() if a["job_id"] == job_id and a["attempt_id"] == attempt_id), None)


def completed_attempts(jobs: list[Job]) -> list[dict]:
    out = []
    for job in jobs:
        try:
            req = job.read_json("request.json")
            enh = job.read_json("enhancement.json") if job.path("enhancement.json").is_file() else {}
            for a in job.list_attempts():
                if a.get("state") == "completed" and a.get("validated"):
                    out.append({"job_id": job.id, "attempt_id": a["id"], "title": enh.get("short_title") or req["prompt"],
                                "triangles": (a.get("mesh") or {}).get("triangles"), "created_at": a.get("created_at"),
                                "glb": f"{job.attempt_dir(a['id'])}/processed/model.glb"})
        except (JobError, OSError, ValueError, KeyError):
            continue
    return out
