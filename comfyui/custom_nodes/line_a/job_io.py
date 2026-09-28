"""Per-job directory, state machine, manifest and logging. No ComfyUI imports (unit-testable)."""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

JOB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")

STATES = [
    "created",
    "prompt_enhanced",
    "prompt_confirmed",
    "candidates_generated",
    "qa_completed",
    "waiting_for_selection",
    "candidate_selected",
    "cutout_completed",
    "model_generated",
    "postprocessed",
    "exported",
    "completed",
]
FAILED = {
    "failed_prompt": "created",
    "failed_generation": "prompt_confirmed",
    "failed_cutout": "candidate_selected",
    "failed_trellis": "cutout_completed",
    "failed_postprocess": "model_generated",
}
# States from which the user may (re)select a candidate and re-run the 3D stages.
SELECTABLE = {"waiting_for_selection", "candidate_selected", "completed", *FAILED} - {
    "failed_prompt",
    "failed_generation",
}


class JobError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(text: str, max_len: int = 24) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "job"


def new_job_id(prompt: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{slugify(prompt)}-{secrets.token_hex(2)}"


def validate_job_id(job_id: str) -> str:
    if not JOB_ID_RE.fullmatch(job_id):
        raise JobError(f"invalid job id: {job_id!r}")
    return job_id


def can_transition(current: str, new: str) -> bool:
    if new in FAILED:
        return current in STATES and current != "completed"
    if new == "candidate_selected":
        return current in SELECTABLE
    if current in FAILED:
        return new == FAILED[current] or STATES.index(new) == STATES.index(FAILED[current]) + 1
    if current not in STATES or new not in STATES:
        return False
    return STATES.index(new) == STATES.index(current) + 1


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Job:
    def __init__(self, output_root: Path, job_id: str) -> None:
        self.id = validate_job_id(job_id)
        self.root = (Path(output_root) / job_id).resolve()
        if not self.root.is_dir():
            raise JobError(f"job not found: {job_id}")

    @classmethod
    def create(cls, output_root: Path, request: dict[str, Any], job_id: str | None = None) -> Job:
        job_id = validate_job_id(job_id or new_job_id(str(request.get("prompt", ""))))
        root = Path(output_root) / job_id
        root.mkdir(parents=True, exist_ok=False)  # never reuse/overwrite an existing job
        job = cls(output_root, job_id)
        job.write_json("request.json", request)
        job.write_text("prompt.txt", str(request.get("prompt", "")))
        job.write_json("job_state.json", {"state": "created", "history": [{"state": "created", "at": now_iso()}]})
        job.write_json("manifest.json", {"job_id": job_id, "created_at": now_iso(), "request": request})
        job.log("job created")
        return job

    def path(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root):
            raise JobError(f"path escapes job dir: {rel!r}")
        return p

    def write_bytes(self, rel: str, data: bytes) -> Path:
        p = self.path(rel)
        atomic_write_bytes(p, data)
        return p

    def write_text(self, rel: str, text: str) -> Path:
        return self.write_bytes(rel, text.encode())

    def write_json(self, rel: str, obj: Any) -> Path:
        return self.write_bytes(rel, (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode())

    def read_json(self, rel: str) -> Any:
        return json.loads(self.path(rel).read_text())

    def log(self, msg: str) -> None:
        with self.path("run.log").open("a") as f:
            f.write(f"{now_iso()} {msg}\n")

    @property
    def state(self) -> str:
        return self.read_json("job_state.json")["state"]

    def set_state(self, new: str, error: str | None = None) -> None:
        data = self.read_json("job_state.json")
        if not can_transition(data["state"], new):
            raise JobError(f"illegal transition {data['state']} -> {new}")
        entry: dict[str, Any] = {"state": new, "at": now_iso()}
        if error:
            entry["error"] = error
        data["state"] = new
        data["history"].append(entry)
        self.write_json("job_state.json", data)
        self.log(f"state -> {new}" + (f" ({error})" if error else ""))

    def update_manifest(self, **fields: Any) -> None:
        manifest = self.read_json("manifest.json")
        for key, value in fields.items():
            if isinstance(value, dict) and isinstance(manifest.get(key), dict):
                manifest[key].update(value)
            else:
                manifest[key] = value
        manifest["updated_at"] = now_iso()
        self.write_json("manifest.json", manifest)
