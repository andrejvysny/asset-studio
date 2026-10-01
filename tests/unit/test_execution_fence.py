"""Execution-mode fence (R15): startup refusal with live work, quiesced auto-record, `execution switch`, admission
pause. Uses real journals; no engines are involved."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from assetstudio_core.ids import new_id
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
    assert j.meta_get(EXECUTION_MODE_KEY) == "nodes" and j.meta_get(ADMISSION_PAUSED_KEY) == "1"  # until activation
    assert j.switch_state()["state"] == "quiesced"
    out = capsys.readouterr().out
    assert "restart Studio with STUDIO_EXECUTION=nodes; admission resumes when it activates" in out


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


# --- handoff: durable switch state + activation generation ------------------------------------------------------
def _old_process(tmp_path: Path) -> tuple[Studio, Coordinator]:
    studio = _studio(tmp_path)
    coord = Coordinator(studio)
    coord.generation = studio.execution_generation = studio.journal.activate("direct")
    return studio, coord


def _quiesce(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, to: str = "nodes") -> None:
    monkeypatch.setenv("STUDIO_INSTANCE_DIR", str(tmp_path / "instance"))
    assert _switch(to) == 0


def _new_process(tmp_path: Path, mode: str = "nodes") -> tuple[Studio, Coordinator]:
    studio = _studio(tmp_path, mode)
    return studio, Coordinator(studio)


def test_old_process_cannot_claim_after_quiesce_even_with_a_stale_cache(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old, coord = _old_process(tmp_path)
    t = _task(old.journal, claim=False)
    _quiesce(tmp_path, monkeypatch)
    coord._pause_read = (time.monotonic(), False)  # the cached hint still says "go"
    assert coord.paused() is False
    assert old.journal.tasks.claim(t, "pas_x", coord.generation) is False  # fenced in the UPDATE itself
    new, ncoord = _new_process(tmp_path)
    assert new.journal.meta_get(ADMISSION_PAUSED_KEY) == "1"  # building a Studio never resumes admission
    gen = new.journal.activate("nodes")
    assert gen == coord.generation + 1 and new.journal.meta_get(ADMISSION_PAUSED_KEY) == "0"
    assert old.journal.tasks.claim(t, "pas_x", coord.generation) is False  # zombie stays fenced after resume
    assert new.journal.tasks.claim(t, "pas_x", gen) is True
    old.close()
    new.close()


def test_coordinator_start_activates_and_a_superseded_process_reports_paused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old, coord = _old_process(tmp_path)
    _quiesce(tmp_path, monkeypatch)
    new, ncoord = _new_process(tmp_path)
    ncoord.start()
    try:
        assert new.execution_generation == ncoord.generation == coord.generation + 1
        assert new.journal.switch_state()["state"] == "active" and new.journal.meta_get(ADMISSION_PAUSED_KEY) == "0"
        coord._pause_read = (float("-inf"), False)
        assert coord.paused() is True
    finally:
        ncoord.stop()
        old.close()
        new.close()


def test_second_switch_is_refused_while_one_is_in_progress(
        instance: Journal, capsys: pytest.CaptureFixture[str]) -> None:
    assert instance.begin_switch("nodes") == "started"
    assert _switch("direct", timeout=0.2) == 2
    assert "already draining" in capsys.readouterr().err
    assert instance.switch_state()["to"] == "nodes"


def test_rerunning_the_same_target_when_quiesced_is_idempotent(
        instance: Journal, capsys: pytest.CaptureFixture[str]) -> None:
    assert _switch("nodes") == 0
    before = instance.switch_state()
    assert _switch("nodes") == 0  # the lost CLI response is re-sent
    assert instance.switch_state() == before  # no second transition
    assert "restart Studio with STUDIO_EXECUTION=nodes" in capsys.readouterr().out
    assert _switch("direct") == 2  # a different target is still refused


def test_abort_from_quiesced_lets_the_old_process_claim_again(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old, coord = _old_process(tmp_path)
    t = _task(old.journal, claim=False)
    gen = coord.generation
    _quiesce(tmp_path, monkeypatch)
    assert old.journal.tasks.claim(t, "pas_x", gen) is False
    assert cli.main(["execution", "abort"]) == 0
    assert old.journal.meta_get(EXECUTION_MODE_KEY) == "direct" and old.journal.generation() == gen
    assert old.journal.switch_state()["state"] == "active" and old.journal.meta_get(ADMISSION_PAUSED_KEY) == "0"
    assert old.journal.tasks.claim(t, "pas_x", gen) is True
    assert cli.main(["execution", "abort"]) == 0  # nothing in progress: harmless
    old.close()


def test_wrong_mode_on_a_quiesced_switch_is_refused_and_stays_paused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _studio(tmp_path).close()
    _quiesce(tmp_path, monkeypatch)
    with pytest.raises(ExecutionModeMismatch) as e:  # build_studio's fence: no auto-adopt of "direct"
        _studio(tmp_path, "direct")
    assert "STUDIO_EXECUTION=nodes" in str(e.value)
    j = Journal(_journal_path(tmp_path))
    with pytest.raises(ExecutionModeMismatch) as e2:
        j.activate("direct")
    assert "assetstudio execution abort" in str(e2.value)
    assert j.meta_get(ADMISSION_PAUSED_KEY) == "1" and j.meta_get(EXECUTION_MODE_KEY) == "nodes"
    assert j.switch_state()["state"] == "quiesced" and j.generation() == 0
    j.close()


def test_activation_refuses_a_draining_switch(instance: Journal) -> None:
    assert instance.begin_switch("nodes") == "started"
    with pytest.raises(ExecutionModeMismatch) as e:
        instance.activate("direct")
    assert "execution abort" in str(e.value) and instance.meta_get(ADMISSION_PAUSED_KEY) == "1"


def test_place_and_accept_are_fenced_for_an_old_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    studio = _studio(tmp_path, "nodes")
    studio.execution_generation = studio.journal.activate("nodes")
    running = _attempt(studio.journal, _task(studio.journal), "offered")  # task running: exempt from the pause
    looked: list[Any] = []
    monkeypatch.setattr(attempts, "eligible_slots", lambda *a, **kw: (looked.append(1), ([], ["none"]))[1])
    studio.journal.activate("nodes")  # a newer process takes over
    attempts.place(studio, running)
    assert looked and studio.journal.attempts.get(running)["runner_id"] is None
    fence = attempts.fence_clause(studio.execution_generation)
    assert studio.journal.attempts.transition(
        running, ("offered",), "offered", runner_id="rnr_x", fence=fence) is False  # what place() relies on
    assert studio.journal.attempts.transition(
        running, ("offered",), "offered", runner_id="rnr_x",
        fence=attempts.fence_clause(studio.journal.generation())) is True
    row = studio.journal.attempts.get(running)
    runner = {"id": "rnr_x", "group_id": "grp"}
    req = attempts.AcceptRequest(session_id=new_id("rse"), generation=row["generation"])
    with pytest.raises(attempts.RunnerError) as e:
        attempts.accept(studio, runner, running, req)
    assert e.value.code == "admission_rejected" and "superseded" in str(e.value)
    studio.close()
