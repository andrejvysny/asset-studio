"""R10: worker3d spool/executor survive per-execution spool and disk errors (torch-free; host pytest)."""
from __future__ import annotations

import errno
import json
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "worker3d"))
import spool  # noqa: E402


class FakeLease:
    def __init__(self) -> None:
        self.left: list[int] = []
        self._n = 0

    def leave(self) -> None:
        self._n += 1
        self.left.append(self._n)


def _ok(eid: str, params: dict[str, Any], body: bytes) -> tuple[bytes, dict[str, Any]]:
    return body[::-1], {"n": len(body)}


@pytest.fixture(autouse=True)
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(spool, "SPOOL", tmp_path)
    return tmp_path


def _submit(root: Path, eid: str, op: str = "generate", body: bytes = b"abc") -> str:
    d = root / eid
    d.mkdir()
    (d / "input.bin").write_bytes(body)
    (d / "request.json").write_text(json.dumps({"op": op, "params": {}}))
    spool.set_state(eid, state="queued")
    return eid


def _drain(runners: dict[str, Any], eids: list[str]) -> tuple[spool.Executor, FakeLease]:
    jobs: queue.Queue[str] = queue.Queue()
    lease = FakeLease()
    ex = spool.Executor(jobs, runners, set(), lease)
    for e in eids:
        jobs.put(e)
    ex.start()
    end = time.monotonic() + 10
    while len(lease.left) < len(eids) and time.monotonic() < end:
        time.sleep(0.01)
    assert len(lease.left) == len(eids), "executor did not finish every execution"
    return ex, lease


def _st(eid: str) -> dict[str, Any]:
    st = spool.state(eid)
    assert st is not None
    return st


def test_happy_path_and_lease_once(root: Path) -> None:
    e = _submit(root, "exec-0001")
    _, lease = _drain({"generate": _ok}, [e])
    assert _st(e)["state"] == "succeeded" and (root / e / "result.bin").read_bytes() == b"cba"
    assert not (root / e / "input.bin").exists() and lease.left == [1]


def test_runner_exception_then_next_execution_runs(root: Path) -> None:
    def boom(eid: str, params: dict[str, Any], body: bytes) -> tuple[bytes, dict[str, Any]]:
        raise RuntimeError("kaput")

    a, b = _submit(root, "exec-0001", "boom"), _submit(root, "exec-0002", "ok")
    _, lease = _drain({"boom": boom, "ok": _ok}, [a, b])
    assert _st(a)["state"] == "failed" and _st(a)["code"] == "internal" and _st(b)["state"] == "succeeded"
    assert len(lease.left) == 2


def test_typed_failure_code_is_kept(root: Path) -> None:
    def oom(eid: str, params: dict[str, Any], body: bytes) -> tuple[bytes, dict[str, Any]]:
        raise spool.ExecutionFailed("oom", "out of GPU memory")

    e = _submit(root, "exec-0001")
    _drain({"generate": oom}, [e])
    assert _st(e)["code"] == "oom"


def test_result_write_enospc_marks_spool_error_and_continues(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = spool.write

    def full(path: Path, data: bytes) -> None:
        if path.parent.name == "exec-0001" and path.name == "result.bin":
            raise OSError(errno.ENOSPC, "No space left on device")
        real(path, data)

    monkeypatch.setattr(spool, "write", full)
    a, b = _submit(root, "exec-0001"), _submit(root, "exec-0002", "second")
    _, lease = _drain({"generate": _ok, "second": _ok}, [a, b])
    assert _st(a)["state"] == "failed" and _st(a)["code"] == "spool_error" and "No space" in _st(a)["error"]
    assert not (root / a / "input.bin").exists()
    assert _st(b)["state"] == "succeeded" and len(lease.left) == 2


def test_corrupt_request_json(root: Path) -> None:
    a, b = _submit(root, "exec-0001"), _submit(root, "exec-0002")
    (root / a / "request.json").write_text("{not json")
    _drain({"generate": _ok}, [a, b])
    assert _st(a)["code"] == "spool_error" and _st(b)["state"] == "succeeded"


def test_unwritable_state_is_logged_and_executor_continues(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = spool.write

    def no_state_for_a(path: Path, data: bytes) -> None:
        if path.parent.name == "exec-0001" and path.name == "state.json":
            raise OSError(errno.EROFS, "read-only")
        real(path, data)

    a, b = _submit(root, "exec-0001"), _submit(root, "exec-0002")
    monkeypatch.setattr(spool, "write", no_state_for_a)
    ex, lease = _drain({"generate": _ok}, [a, b])
    assert _st(b)["state"] == "succeeded" and len(lease.left) == 2 and ex.alive()


def test_corrupt_state_reported_lost_and_recover_survives(root: Path) -> None:
    a, b = _submit(root, "exec-0001"), _submit(root, "exec-0002")
    (root / a / "state.json").write_text("\x00garbage")
    st = _st(a)
    assert st["state"] == "lost" and st["code"] == "spool_corrupt" and st["error"]
    (root / b / "state.json").write_text(json.dumps({"state": "running", "session_id": "s"}))
    spool.recover()
    assert _st(b)["state"] == "lost" and _st(a)["code"] == "spool_corrupt"
    assert json.loads((root / a / "state.json").read_text())["state"] == "lost"  # persisted terminal marker


def test_cancelled_before_start(root: Path) -> None:
    e = _submit(root, "exec-0001")
    jobs: queue.Queue[str] = queue.Queue()
    lease = FakeLease()
    ex = spool.Executor(jobs, {"generate": _ok}, {e}, lease)
    jobs.put(e)
    ex.start()
    end = time.monotonic() + 10
    while not lease.left and time.monotonic() < end:
        time.sleep(0.01)
    assert _st(e)["state"] == "cancelled" and lease.left == [1] and e not in ex.cancel_flags


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_dead_executor_thread_is_restarted_and_counted(root: Path) -> None:
    class Fatal(BaseException):
        pass

    def die(eid: str, params: dict[str, Any], body: bytes) -> tuple[bytes, dict[str, Any]]:
        raise Fatal()

    jobs: queue.Queue[str] = queue.Queue()
    lease = FakeLease()
    ex = spool.Executor(jobs, {"generate": die}, set(), lease)
    ex.start()
    jobs.put(_submit(root, "exec-0001"))
    t = ex._thread
    assert t is not None
    threading.Thread.join(t, 10)
    assert not t.is_alive()
    h = ex.health()
    assert h["alive"] and h["restarts"] == 1 and h["last_error"]
    ex.runners["generate"] = _ok
    jobs.put(_submit(root, "exec-0002"))
    end = time.monotonic() + 10
    while len(lease.left) < 2 and time.monotonic() < end:
        time.sleep(0.01)
    assert _st("exec-0002")["state"] == "succeeded"
