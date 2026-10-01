"""Runner-local durable state: identity and attempts. WAL + synchronous=FULL so a recorded attempt survives a crash."""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from assetstudio_protocol.execution import Offer
from assetstudio_protocol.runners import LocalAttempt

from .spool import Spool

_SCHEMA = """
CREATE TABLE IF NOT EXISTS identity(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempts(
    attempt_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, offer TEXT NOT NULL, state TEXT NOT NULL,
    manifest TEXT, error TEXT, engine_execution_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS epochs(slot_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL);
"""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class AttemptRow:
    attempt_id: str
    generation: int
    offer: Offer
    state: str
    manifest: str | None
    error: str | None
    engine_execution_id: str | None = None


class RunnerState:
    def __init__(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(state_dir / "runner.sqlite", check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        if "engine_execution_id" not in {r[1] for r in self._db.execute("PRAGMA table_info(attempts)")}:
            self._db.execute("ALTER TABLE attempts ADD COLUMN engine_execution_id TEXT")  # pre-H12 databases

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def get_identity(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM identity WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_identity(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO identity(key, value) VALUES(?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def next_epoch(self, slot_id: str) -> int:
        """Monotonic GPU-lane epoch per slot, durable across restarts (R7): one atomic statement."""
        with self._lock:
            row = self._db.execute(
                "INSERT INTO epochs(slot_id, epoch) VALUES(?, 1) "
                "ON CONFLICT(slot_id) DO UPDATE SET epoch=epoch+1 RETURNING epoch", (slot_id,)).fetchone()
        return int(row[0])

    def record_attempt(self, offer: Offer) -> bool:
        """Insert-if-absent. False means the attempt is already local (duplicate delivery)."""
        now = _now()
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO attempts(attempt_id, generation, offer, state, created_at, updated_at) "
                "VALUES(?, ?, ?, 'admitted', ?, ?)",
                (offer.attempt_id, offer.generation, offer.model_dump_json(), now, now))
            return cur.rowcount == 1

    @staticmethod
    def _row(r: tuple) -> AttemptRow:
        return AttemptRow(r[0], r[1], Offer.model_validate_json(r[2]), r[3], r[4], r[5], r[6])

    _COLS = "attempt_id, generation, offer, state, manifest, error, engine_execution_id"

    def get_attempt(self, attempt_id: str) -> AttemptRow | None:
        with self._lock:
            r = self._db.execute(f"SELECT {self._COLS} FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        return self._row(r) if r else None

    def list_attempts(self) -> list[AttemptRow]:
        with self._lock:
            rows = self._db.execute(f"SELECT {self._COLS} FROM attempts ORDER BY created_at").fetchall()
        return [self._row(r) for r in rows]

    def set_state(self, attempt_id: str, state: str, *, manifest: str | None = None, error: str | None = None,
                  engine_execution_id: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE attempts SET state=?, manifest=COALESCE(?, manifest), error=COALESCE(?, error), "
                "engine_execution_id=COALESCE(?, engine_execution_id), updated_at=? WHERE attempt_id=?",
                (state, manifest, error, engine_execution_id, _now(), attempt_id))

    def local_attempts(self, spool: Spool) -> list[LocalAttempt]:
        out = []
        for a in self.list_attempts():
            m = spool.manifest(a.attempt_id)
            out.append(LocalAttempt(attempt_id=a.attempt_id, generation=a.generation, state=a.state,
                                    spooled=list(m.files) if m else []))
        return out

    def delete_attempt(self, attempt_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM attempts WHERE attempt_id=?", (attempt_id,))
