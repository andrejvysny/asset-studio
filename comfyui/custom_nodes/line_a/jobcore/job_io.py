"""Per-job directory, state machine, locking, operations, attempts, manifest. No ComfyUI imports (unit-testable)."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

JOB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
ATTEMPT_RE = re.compile(r"^att-\d{2,4}$")

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
# States from which the user may approve a candidate (creates a new 3D attempt).
SELECTABLE = {"waiting_for_selection", "candidate_selected", "completed", *FAILED} - {
    "failed_prompt",
    "failed_generation",
}


class JobError(Exception):
    pass


class JobConflict(JobError):
    """Request is valid but conflicts with current job state (HTTP 409)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(text: str, max_len: int = 24) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "job"


def new_job_id(prompt: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{slugify(prompt)}-{secrets.token_hex(2)}"


def random_seed_family() -> int:
    return 1 + secrets.randbelow(2**31 - 1000)


def validate_job_id(job_id: str) -> str:
    if not JOB_ID_RE.fullmatch(job_id):
        raise JobError(f"invalid job id: {job_id!r}")
    return job_id


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def can_transition(current: str, new: str) -> bool:
    if new not in STATES and new not in FAILED:
        return False
    if new in FAILED:
        return current in STATES and current != "completed"
    if new == "candidate_selected":
        return current in SELECTABLE
    if current in FAILED:
        return new == FAILED[current] or STATES.index(new) == STATES.index(FAILED[current]) + 1
    if current not in STATES:
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


class _ReentrantFileLock:
    """flock on <job>/.lock, re-entrant within a thread (flock alone would self-deadlock on nested use)."""

    _registry: dict[Path, _ReentrantFileLock] = {}
    _registry_guard = threading.Lock()

    def __init__(self, path: Path) -> None:
        self.path = path
        self._rlock = threading.RLock()
        self._depth = 0
        self._fd: int | None = None

    @classmethod
    def for_path(cls, path: Path) -> _ReentrantFileLock:
        with cls._registry_guard:
            return cls._registry.setdefault(path, cls(path))

    @contextmanager
    def hold(self) -> Iterator[None]:
        with self._rlock:
            if self._depth == 0:
                self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
                fcntl.flock(self._fd, fcntl.LOCK_EX)
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0 and self._fd is not None:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                    os.close(self._fd)
                    self._fd = None


class Job:
    def __init__(self, output_root: Path, job_id: str) -> None:
        self.id = validate_job_id(job_id)
        self.output_root = Path(output_root).resolve()
        self.root = (self.output_root / job_id).resolve()
        # Symlinked job dirs could point outside the output root.
        if not self.root.is_relative_to(self.output_root) or self.root.parent != self.output_root:
            raise JobError(f"job dir escapes output root: {job_id}")
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
        job.write_json("job_state.json", {"state": "created", "active_operation": None,
                                          "history": [{"state": "created", "at": now_iso()}]})
        job.write_json("manifest.json", {"job_id": job_id, "created_at": now_iso(), "request": request})
        job.log("job created")
        return job

    # --- files -----------------------------------------------------------------------------------
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

    # --- locking / state -------------------------------------------------------------------------
    @contextmanager
    def locked(self) -> Iterator[None]:
        with _ReentrantFileLock.for_path(self.path(".lock")).hold():
            yield

    @property
    def state(self) -> str:
        return self.read_json("job_state.json")["state"]

    @property
    def active_operation(self) -> dict | None:
        return self.read_json("job_state.json").get("active_operation")

    def require_state(self, allowed: set[str] | str) -> None:
        allowed = {allowed} if isinstance(allowed, str) else allowed
        if self.state not in allowed:
            raise JobConflict(f"job {self.id} is in state {self.state}, expected {sorted(allowed)}")

    def set_state(self, new: str, error: str | None = None) -> None:
        with self.locked():
            data = self.read_json("job_state.json")
            if not can_transition(data["state"], new):
                raise JobConflict(f"illegal transition {data['state']} -> {new}")
            entry: dict[str, Any] = {"state": new, "at": now_iso()}
            if error:
                entry["error"] = error
            data["state"] = new
            data["history"].append(entry)
            self.write_json("job_state.json", data)
        self.log(f"state -> {new}" + (f" ({error})" if error else ""))

    @contextmanager
    def operation(self, name: str, allowed_states: set[str] | str) -> Iterator[str]:
        """Exclusive long-running operation: state is validated before any side effect."""
        with self.locked():
            data = self.read_json("job_state.json")
            if data.get("active_operation"):
                op = data["active_operation"]
                raise JobConflict(f"job {self.id} busy: {op['name']} since {op['started_at']}")
            self.require_state(allowed_states)
            op_id = secrets.token_hex(4)
            data["active_operation"] = {"id": op_id, "name": name, "started_at": now_iso()}
            self.write_json("job_state.json", data)
        try:
            yield op_id
        finally:
            with self.locked():
                data = self.read_json("job_state.json")
                if (data.get("active_operation") or {}).get("id") == op_id:
                    data["active_operation"] = None
                    self.write_json("job_state.json", data)

    def clear_stale_operation(self, reason: str) -> bool:
        with self.locked():
            data = self.read_json("job_state.json")
            op = data.get("active_operation")
            if not op:
                return False
            data["active_operation"] = None
            data["history"].append({"state": data["state"], "at": now_iso(),
                                    "error": f"operation {op['name']} interrupted: {reason}"})
            self.write_json("job_state.json", data)
        self.log(f"cleared interrupted operation {op['name']} ({reason})")
        return True

    def update_manifest(self, **fields: Any) -> None:
        with self.locked():
            manifest = self.read_json("manifest.json")
            for key, value in fields.items():
                if isinstance(value, dict) and isinstance(manifest.get(key), dict):
                    manifest[key].update(value)
                else:
                    manifest[key] = value
            manifest["updated_at"] = now_iso()
            self.write_json("manifest.json", manifest)

    # --- 3D attempts -----------------------------------------------------------------------------
    def attempt_dir(self, attempt_id: str) -> str:
        if not ATTEMPT_RE.fullmatch(attempt_id):
            raise JobError(f"invalid attempt id {attempt_id!r}")
        return f"model/attempts/{attempt_id}"

    def list_attempts(self) -> list[dict]:
        base = self.path("model/attempts")
        if not base.is_dir():
            return []
        out = []
        for d in sorted(base.iterdir()):
            if ATTEMPT_RE.fullmatch(d.name) and (d / "attempt.json").is_file():
                out.append(json.loads((d / "attempt.json").read_text()))
        return out

    def new_attempt(self, fields: dict[str, Any]) -> dict:
        """Create model/attempts/att-NN with a monotonic, never-reused number. Caller holds the lock."""
        with self.locked():
            existing = [a["id"] for a in self.list_attempts()]
            n = 1 + max((int(a.split("-")[1]) for a in existing), default=0)
            attempt = {"id": f"att-{n:02d}", "created_at": now_iso(), "state": "approved", **fields}
            self.path(self.attempt_dir(attempt["id"])).mkdir(parents=True, exist_ok=False)
            self.write_json(f"{self.attempt_dir(attempt['id'])}/attempt.json", attempt)
            return attempt

    def read_attempt(self, attempt_id: str) -> dict:
        return self.read_json(f"{self.attempt_dir(attempt_id)}/attempt.json")

    def update_attempt(self, attempt_id: str, **fields: Any) -> dict:
        with self.locked():
            attempt = self.read_attempt(attempt_id)
            attempt.update(fields)
            attempt["updated_at"] = now_iso()
            self.write_json(f"{self.attempt_dir(attempt_id)}/attempt.json", attempt)
            return attempt
