"""Persisted run-level intent (run | paused | cancelled | closed): the durable authority every admission consults.

No row means `run`, so the table needs no data migration. Task-level `control` stays a per-task mirror; this is
what survives a run with zero active tasks (e.g. paused at a human gate) and what new tasks are admitted under.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from assetstudio_core.canonical import now_iso

RUN_CONTROLS = ("run", "paused", "cancelled", "closed")

DDL = """
CREATE TABLE IF NOT EXISTS run_controls (
  run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, control TEXT NOT NULL, revision INTEGER NOT NULL,
  updated_at TEXT NOT NULL);
"""

# Tasks of paused/cancelled runs are never dispatched (a running task may finish).
NOT_HELD_SQL = ("(run_id IS NULL OR run_id NOT IN (SELECT run_id FROM run_controls "
                "WHERE control IN ('paused','cancelled')))")


class RunNotOpen(Exception):
    """The run was cancelled or closed: it accepts no new work."""

    code = "run_not_open"

    def __init__(self, run_id: str, control: str) -> None:
        super().__init__(f"run {run_id} is {control}")
        self.run_id, self.control = run_id, control


def read_control(db: sqlite3.Connection, run_id: str) -> tuple[str, int]:
    r = db.execute("SELECT control, revision FROM run_controls WHERE run_id=?", (run_id,)).fetchone()
    return (r["control"], r["revision"]) if r else ("run", 0)


def write_control(db: sqlite3.Connection, project_id: str, run_id: str, control: str) -> int:
    if control not in RUN_CONTROLS:
        raise ValueError(f"unknown run control {control!r}")
    db.execute(
        "INSERT INTO run_controls (run_id, project_id, control, revision, updated_at) VALUES (?,?,?,1,?) "
        "ON CONFLICT(run_id) DO UPDATE SET control=excluded.control, revision=revision+1, "
        "updated_at=excluded.updated_at", (run_id, project_id, control, now_iso()))
    return read_control(db, run_id)[1]


class RunControlMixin:
    _db: sqlite3.Connection
    _lock: threading.RLock
    txn: Callable[[], AbstractContextManager[sqlite3.Connection]]

    @staticmethod
    def _admit_runs(db: sqlite3.Connection, tasks: list[Any]) -> dict[str | None, str]:
        """Run control per distinct run, read inside the creating transaction: no work enters a closed or
        cancelled run; a paused run admits it paused."""
        out: dict[str | None, str] = {}
        for run_id in {t.run_id for t in tasks if t.run_id}:
            control = read_control(db, run_id)[0]
            if control in ("cancelled", "closed"):
                raise RunNotOpen(run_id, control)
            out[run_id] = control
        return out

    def run_control(self, run_id: str) -> str:
        with self._lock:
            return read_control(self._db, run_id)[0]

    def run_control_revision(self, run_id: str) -> int:
        with self._lock:
            return read_control(self._db, run_id)[1]

    def set_run_control(self, project_id: str, run_id: str, control: str,
                        db: sqlite3.Connection | None = None) -> int:
        if db is None:
            with self.txn() as conn:
                return write_control(conn, project_id, run_id, control)
        return write_control(db, project_id, run_id, control)
