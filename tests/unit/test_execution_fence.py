"""Execution-mode fence (R15): startup refusal with live work, quiesced auto-record, `execution switch`, admission
pause. Uses real journals; no engines are involved."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from assetstudio_server import cli
from assetstudio_server.coordinator.runner import Coordinator
from assetstudio_server.journal import ADMISSION_PAUSED_KEY, EXECUTION_MODE_KEY, Journal
from assetstudio_server.services import attempts
from assetstudio_server.studio import ExecutionModeMismatch, Studio, build_studio
from assetstudio_server.taskstore import NewTask

from tests.conftest import make_settings


def _studio(tmp_path: Path, mode: str = "direct") -> Studio:
    s = make_settings(tmp_path, coordinator=False)
    s.execution = mode
    return build_studio(s)


def _journal_path(tmp_path: Path) -> Path:
    return tmp_path / "instance" / "journal" / "operations.sqlite"


def _task(j: Journal, n: int = 1, claim: bool = True) -> str:
    t = NewTask(project_id="prj_x", job_id=f"job_{n:0>16}", item_id=f"itm_{n:0>16}", stage="generate",
                family="generate", input_key=str(n), inputs={}, lane="gpu1", residency="x")
    j.tasks.create([t], f"cmd_{n}")
    if claim:
        assert j.tasks.claim(t.id, "pas_x")
    return t.id


def _attempt(j: Journal, task_id: str, state: str) -> str:
    row, _ = j.attempts.create_offer(
        task_id=task_id, call_key="k", generation=1, project_id="prj_x", operation="image.t2i", operation_version=1,
        input_digest="d", offer={"requirements": {"capability": "image", "engine": "comfyui"}}, runner_id=None, session_id=None, slot_id=None, offer_expires_at=None)
    if state != "offered":
        assert j.attempts.transition(row["id"], ("offered",), state)
    return row["id"]


def test_fresh_direct_instance_writes_no_mode_row(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    assert studio.journal.meta_get(EXECUTION_MODE_KEY) is None
    studio.close()


def test_nodes_on_a_quiesced_journal_is_recorded(tmp_path: Path) -> None:
    _studio(tmp_path).close()
    studio = _studio(tmp_path, "nodes")
    assert studio.journal.meta_get(EXECUTION_MODE_KEY) == "nodes"
    studio.close()
    again = _studio(tmp_path, "nodes")  # same mode: no refusal, nothing changes
    assert again.journal.meta_get(EXECUTION_MODE_KEY) == "nodes"
    again.close()


def test_direct_against_a_nodes_journal_with_a_leased_attempt_is_refused(tmp_path: Path) -> None:
    studio = _studio(tmp_path, "nodes")
    _attempt(studio.journal, _task(studio.journal), "leased")
    studio.close()
    with pytest.raises(ExecutionModeMismatch) as e:
        _studio(tmp_path, "direct")
    msg = str(e.value)
    assert "'nodes'" in msg and "'direct'" in msg and "1 running" in msg and "1 open attempts" in msg
    assert "assetstudio execution switch --to direct" in msg
    j = Journal(_journal_path(tmp_path))
    assert j.meta_get(EXECUTION_MODE_KEY) == "nodes"  # a refusal never rewrites the mode
    j.close()


def test_running_or_queued_tasks_also_fence_a_fresh_nodes_start(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    _task(studio.journal, 1, claim=False)  # queued, runnable
    studio.close()
    with pytest.raises(ExecutionModeMismatch):
        _studio(tmp_path, "nodes")


def test_direct_with_only_terminal_attempts_is_allowed_and_recorded(tmp_path: Path) -> None:
    studio = _studio(tmp_path, "nodes")
    tid = _task(studio.journal)
    _attempt(studio.journal, tid, "cancelled")
    studio.journal.tasks.finish(tid, "failed", error={"code": "x", "message": "x"})
    studio.close()
    back = _studio(tmp_path, "direct")
    assert back.journal.meta_get(EXECUTION_MODE_KEY) == "direct"
    back.close()


def _switch(to: str, timeout: float = 5.0) -> int:
    return cli.main(["execution", "switch", "--to", to, "--timeout", str(timeout), "--poll", "0.05"])


@pytest.fixture
def instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Journal:
    monkeypatch.setenv("STUDIO_INSTANCE_DIR", str(tmp_path / "instance"))
    monkeypatch.setenv("STUDIO_EXECUTION", "direct")
    j = Journal(_journal_path(tmp_path))
    j.meta_set("unrelated", "1")  # the directory exists
    return j


def test_switch_waits_for_quiescence_then_flips(instance: Journal, capsys: pytest.CaptureFixture[str]) -> None:
    j = instance
    tid = _task(j)

    def finish_later() -> None:
        time.sleep(0.4)
        assert j.meta_get(ADMISSION_PAUSED_KEY) == "1"  # paused while it waits
        j.tasks.finish(tid, "succeeded", result={})

    t = threading.Thread(target=finish_later)
    t.start()
    assert _switch("nodes") == 0
    t.join()
    assert j.meta_get(EXECUTION_MODE_KEY) == "nodes" and j.meta_get(ADMISSION_PAUSED_KEY) == "0"
    assert "switched to nodes; restart Studio with STUDIO_EXECUTION=nodes" in capsys.readouterr().out


def test_switch_timeout_exits_3_restores_admission_and_changes_nothing(
        instance: Journal, capsys: pytest.CaptureFixture[str]) -> None:
    _attempt(instance, _task(instance), "executing")
    assert _switch("nodes", timeout=0.3) == 3
    assert instance.meta_get(EXECUTION_MODE_KEY) is None and instance.meta_get(ADMISSION_PAUSED_KEY) == "0"
    err = capsys.readouterr().err
    assert "nothing changed" in err and "'attempts': 1" in err


def test_status_reports_modes_live_work_and_pause(instance: Journal, capsys: pytest.CaptureFixture[str]) -> None:
    _task(instance)
    instance.meta_set(ADMISSION_PAUSED_KEY, "1")
    assert cli.main(["execution", "status"]) == 0
    out = capsys.readouterr().out
    assert '"persisted_mode": "direct"' in out and '"configured_mode": "direct"' in out
    assert '"admission_paused": true' in out and '"running": 1' in out


def test_pause_stops_the_coordinator_from_picking(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    _task(studio.journal, claim=False)
    coord = Coordinator(studio)
    assert coord.choose("gpu1") is not None
    studio.journal.meta_set(ADMISSION_PAUSED_KEY, "1")
    assert coord.choose("gpu1") is not None  # the pause flag is cached for up to a second
    coord._pause_read = (float("-inf"), False)
    assert coord.choose("gpu1") is None
    studio.journal.meta_set(ADMISSION_PAUSED_KEY, "0")
    coord._pause_read = (float("-inf"), False)
    assert coord.choose("gpu1") is not None
    studio.close()


def test_pause_blocks_attempt_placement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    studio = _studio(tmp_path, "nodes")
    aid = _attempt(studio.journal, _task(studio.journal, claim=False), "offered")  # its task has not started
    looked: list[Any] = []

    def spy(*a: Any, **kw: Any) -> tuple[list[Any], list[str]]:
        looked.append(kw)
        return [], ["none"]

    monkeypatch.setattr(attempts, "eligible_slots", spy)
    studio.journal.meta_set(ADMISSION_PAUSED_KEY, "1")
    assert attempts.place(studio, aid) is False and looked == []
    assert attempts.place_pending(studio) == 0 and looked == []
    studio.journal.meta_set(ADMISSION_PAUSED_KEY, "0")
    attempts.place(studio, aid)
    assert len(looked) == 1  # the same call reaches placement once admission resumes
    running = _attempt(studio.journal, _task(studio.journal, 2), "offered")
    studio.journal.meta_set(ADMISSION_PAUSED_KEY, "1")
    attempts.place(studio, running)
    assert len(looked) == 2  # a task already running may finish its calls while a switch drains
    studio.close()
