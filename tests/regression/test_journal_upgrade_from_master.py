"""A journal written by master (schema v3, Studio crashed mid-generation) upgrades to v4 in place: no row changes,
a reopen is a no-op, and direct-mode restart recovery does exactly what master did (fixture README)."""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from assetstudio_server.journal import Journal

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "journal_v3_master"
V3_TABLES = ("operations", "commands", "scoped_commands", "lane_epochs", "stage_tasks", "passes", "command_intents",
             "run_controls")
V4_TABLES = {"journal_meta", "runner_sessions", "devices", "slots", "attempts", "attempt_events", "uploads"}


def _copy(tmp_path: Path) -> Path:
    path = tmp_path / "operations.sqlite"
    shutil.copyfile(FIXTURE / "operations.sqlite", path)
    return path


def _inspect(path: Path) -> tuple[int, set[str], dict[str, list[tuple]]]:
    db = sqlite3.connect(path)
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows = {t: db.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in V3_TABLES}
        return version, tables, rows
    finally:
        db.close()


def test_fixture_is_a_master_v3_journal(tmp_path: Path) -> None:
    version, tables, rows = _inspect(_copy(tmp_path))
    assert version == 3 and not tables & V4_TABLES
    assert rows["stage_tasks"] and rows["scoped_commands"] and rows["lane_epochs"]
    assert any(r for r in rows["stage_tasks"] if "running" in r)


def test_upgrade_keeps_every_row_and_reopen_is_a_no_op(tmp_path: Path) -> None:
    path = _copy(tmp_path)
    _, _, before = _inspect(path)
    Journal(path).close()
    version, tables, after = _inspect(path)
    assert version == 4 and V4_TABLES <= tables and after == before
    Journal(path).close()
    assert _inspect(path) == (version, tables, after)


def test_direct_mode_recovery_matches_master(tmp_path: Path) -> None:
    expected = json.loads((FIXTURE / "expected_recovery.json").read_text())
    j = Journal(_copy(tmp_path))
    try:
        result = j.tasks.recover_after_restart()
        assert {k: v for k, v in result.items() if k != "orphans"} == expected["result"]
        assert sorted(o["id"] for o in result["orphans"]) == expected["orphans"]
        got = [list(r) for r in j._db.execute(
            "SELECT id, state, control, error, progress FROM stage_tasks ORDER BY id")]
        assert got == expected["tasks"]
        lanes = dict(j._db.execute("SELECT lane, epoch FROM lane_epochs").fetchall())
        assert all(j.next_epoch(lane) == epoch + 1 for lane, epoch in lanes.items())
    finally:
        j.close()
