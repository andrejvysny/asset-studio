"""Torch-free spool + executor for worker3d (Python 3.10+). Tested on the host without the GPU stack.

One execution's failure (runner error, full disk, corrupt spool file) is contained to that execution: it is
recorded as its state when possible, otherwise logged, and the executor thread always continues.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("worker3d.spool")

SPOOL = Path(os.environ.get("WORKER3D_SPOOL", "/spool"))
EXEC_ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
TERMINAL = ("succeeded", "failed", "cancelled", "lost")

Runner = Callable[[str, dict[str, Any], bytes], "tuple[bytes, dict[str, Any]]"]


class ExecutionFailed(Exception):
    """A runner's typed failure (code is reported to the Studio)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".tmp-{path.name}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def state(eid: str) -> dict[str, Any] | None:
    """None if unknown; a terminal `lost/spool_corrupt` record if state.json cannot be read or parsed."""
    try:
        st = json.loads((SPOOL / eid / "state.json").read_text())
        if not isinstance(st, dict):
            raise ValueError("state.json is not an object")
        return st
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        err = f"unreadable state.json: {type(e).__name__}: {e}"
        return {"state": "lost", "code": "spool_corrupt", "error": err[:300]}


def set_state(eid: str, **fields: Any) -> None:
    st = {**(state(eid) or {}), **fields, "updated_at": time.time()}
    write(SPOOL / eid / "state.json", json.dumps(st).encode())


def recover() -> None:
    """Executions that were queued/running when a previous process died can no longer complete."""
    SPOOL.mkdir(parents=True, exist_ok=True)
    for d in SPOOL.iterdir():
        if not EXEC_ID.fullmatch(d.name):
            continue
        try:
            st = state(d.name)
            if st is None:
                continue
            if st.get("code") == "spool_corrupt":
                set_state(d.name, **st)  # persist the marker so the record is terminal on disk too
            elif st.get("state") not in TERMINAL:
                set_state(d.name, state="lost", error="worker restarted before this execution finished",
                          lost_session=st.get("session_id"))
        except Exception:
            log.exception("could not recover spool entry %s", d.name)


def _fail(eid: str, exc: BaseException) -> None:
    try:
        set_state(eid, state="failed", code="spool_error", error=f"{type(exc).__name__}: {str(exc)[:300]}")
    except Exception:
        log.exception("execution %s: could not record spool_error (original: %r)", eid, exc)


def _run(eid: str, runners: dict[str, Runner], cancelled: Callable[[str], bool]) -> None:
    d = SPOOL / eid
    req = json.loads((d / "request.json").read_text())
    runner = runners[req["op"]]
    body = (d / "input.bin").read_bytes()
    if cancelled(eid):
        set_state(eid, state="cancelled")
        return
    set_state(eid, state="running", started_at=time.time())
    try:
        data, meta = runner(eid, req["params"], body)
    except InterruptedError:
        set_state(eid, state="cancelled")
        return
    except ExecutionFailed as e:
        set_state(eid, state="failed", error=str(e), code=e.code)
        return
    except Exception as e:  # recorded, never swallowed; the executor keeps serving
        set_state(eid, state="failed", error=f"{type(e).__name__}: {str(e)[:300]}", code="internal")
        return
    write(d / "result.bin", data)
    write(d / "meta.json", json.dumps(meta, separators=(",", ":")).encode())
    set_state(eid, state="succeeded", finished_at=time.time(),
              result_sha256=hashlib.sha256(data).hexdigest(), result_size=len(data))


def process(eid: str, runners: dict[str, Runner], cancelled: Callable[[str], bool]) -> None:
    """Run one execution end to end. Never raises for spool/disk problems."""
    try:
        _run(eid, runners, cancelled)
    except Exception as e:
        _fail(eid, e)
    finally:
        try:
            (SPOOL / eid / "input.bin").unlink(missing_ok=True)
        except OSError:
            log.exception("execution %s: could not remove input", eid)


class Executor:
    """One GPU execution at a time on a supervised thread. Activity was counted at admission (lease.enter) and
    is released here exactly once per execution."""

    def __init__(self, jobs: queue.Queue[str], runners: dict[str, Runner], cancel_flags: set[str], lease: Any) -> None:
        self.jobs, self.runners, self.cancel_flags, self.lease = jobs, runners, cancel_flags, lease
        self.restarts = 0
        self.last_error: str | None = None
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()

    def _step(self) -> None:
        eid = self.jobs.get()
        try:
            process(eid, self.runners, lambda e: e in self.cancel_flags)
        finally:
            self.cancel_flags.discard(eid)
            self.lease.leave()

    def _loop(self) -> None:
        while True:
            try:
                self._step()
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {str(e)[:300]}"
                log.exception("executor step failed; continuing")

    def _spawn(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="executor", daemon=True)
        self._thread.start()

    def start(self) -> None:
        with self._guard:
            if self._thread is None:
                self._spawn()

    def alive(self) -> bool:
        """Restarts a dead executor thread (counted) and reports liveness."""
        with self._guard:
            if self._thread is not None and not self._thread.is_alive():
                self.restarts += 1
                self.last_error = self.last_error or "executor thread died"
                self._spawn()
            return self._thread is not None and self._thread.is_alive()

    def health(self) -> dict[str, Any]:
        alive = self.alive()
        return {"alive": alive, "restarts": self.restarts, "last_error": self.last_error}
