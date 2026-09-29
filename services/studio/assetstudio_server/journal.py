"""Durable local operation journal (SQLite, instance dir, single host) + command idempotency records.

Non-disposable: this is live dispatch state, kept separate from the rebuildable search index.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from assetstudio_core.canonical import now_iso, sha256_json
from assetstudio_core.ids import new_id

ACTIVE_STATES = ("held", "queued", "running", "cancel_requested", "reconciling")
TERMINAL_STATES = ("succeeded", "failed", "cancelled")

_DDL = """
CREATE TABLE IF NOT EXISTS operations (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, batch_id TEXT, kind TEXT NOT NULL, lane TEXT NOT NULL,
  affinity TEXT NOT NULL, state TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, payload_hash TEXT NOT NULL,
  payload TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '{}', result TEXT, error TEXT,
  engine TEXT NOT NULL DEFAULT '{}',
  attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, claimed_by TEXT,
  heartbeat_at TEXT);
CREATE INDEX IF NOT EXISTS ops_lane_state ON operations(lane, state);
CREATE INDEX IF NOT EXISTS ops_batch ON operations(project_id, batch_id);
CREATE TABLE IF NOT EXISTS commands (
  key TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL);
"""
# v2: client idempotency keys are scoped by project + action family (a key reused in another project or for another
# action is a different command). v1 rows stay readable under project '' / action 'legacy' and never alias new ones.
_V2 = """
CREATE TABLE IF NOT EXISTS scoped_commands (
  project_id TEXT NOT NULL, action TEXT NOT NULL, key TEXT NOT NULL, payload_hash TEXT NOT NULL,
  response TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (project_id, action, key));
INSERT OR IGNORE INTO scoped_commands SELECT '', 'legacy', key, payload_hash, response, created_at FROM commands;
"""
# v3: monotonic GPU-lane fencing epochs survive Studio restarts (a restarted Studio never reuses an epoch).
_V3 = """
CREATE TABLE IF NOT EXISTS lane_epochs (lane TEXT PRIMARY KEY, epoch INTEGER NOT NULL);
"""
_MIGRATIONS = ((2, _V2), (3, _V3))


class IdempotencyConflict(Exception):
    code = "idempotency_conflict"


@dataclass
class Operation:
    id: str
    project_id: str
    batch_id: str | None
    kind: str
    lane: str
    affinity: str
    state: str
    idempotency_key: str
    payload: dict[str, Any]
    progress: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    engine: dict[str, Any]
    attempts: int
    created_at: str
    updated_at: str

    def public(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k not in ("idempotency_key",)}


def _op(row: sqlite3.Row) -> Operation:
    return Operation(
        id=row["id"], project_id=row["project_id"], batch_id=row["batch_id"], kind=row["kind"], lane=row["lane"],
        affinity=row["affinity"], state=row["state"], idempotency_key=row["idempotency_key"],
        payload=json.loads(row["payload"]), progress=json.loads(row["progress"]),
        result=json.loads(row["result"]) if row["result"] else None,
        error=json.loads(row["error"]) if row["error"] else None, engine=json.loads(row["engine"]),
        attempts=row["attempts"], created_at=row["created_at"], updated_at=row["updated_at"])


class Journal:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_DDL)
        for version, script in _MIGRATIONS:
            if self._db.execute("PRAGMA user_version").fetchone()[0] < version:
                self._db.executescript(f"BEGIN;{script}PRAGMA user_version={version}; COMMIT;")
        self._lock = threading.RLock()
        self.changed = threading.Condition(self._lock)

    def close(self) -> None:
        self._db.close()

    def enqueue(self, *, project_id: str, kind: str, lane: str, affinity: str, payload: dict[str, Any],
                idempotency_key: str, batch_id: str | None = None, hold: bool = False) -> tuple[Operation, bool]:
        """hold=True creates the op unclaimable until release(): callers mark items first, then release.
        The client key is scoped by project and operation kind before it is stored."""
        idempotency_key = f"{project_id}/{kind}/{idempotency_key}"
        phash = sha256_json({"kind": kind, "payload": payload})
        with self._lock:
            row = self._db.execute("SELECT * FROM operations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row is not None:
                if row["payload_hash"] != phash:
                    raise IdempotencyConflict("idempotency key reused with a different request")
                return _op(row), False
            now = now_iso()
            op_id = new_id("op")
            self._db.execute(
                "INSERT INTO operations (id, project_id, batch_id, kind, lane, affinity, state, idempotency_key, "
                "payload_hash, payload, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (op_id, project_id, batch_id, kind, lane, affinity, "held" if hold else "queued", idempotency_key,
                 phash, json.dumps(payload), now, now))
            self.changed.notify_all()
            return self.get(op_id), True  # type: ignore[return-value]

    def release(self, op_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE operations SET state='queued', updated_at=? WHERE id=? AND state='held'",
                             (now_iso(), op_id))
            self.changed.notify_all()

    def release_all_held(self) -> int:
        with self._lock:
            n = self._db.execute("UPDATE operations SET state='queued', updated_at=? WHERE state='held'",
                                 (now_iso(),)).rowcount
            self.changed.notify_all()
            return n

    def get(self, op_id: str) -> Operation | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
        return _op(row) if row else None

    def list(self, *, project_id: str | None = None, batch_id: str | None = None,
             states: tuple[str, ...] | None = None, lane: str | None = None, limit: int = 500) -> list[Operation]:
        where, args = [], []
        for col, val in (("project_id", project_id), ("batch_id", batch_id), ("lane", lane)):
            if val is not None:
                where.append(f"{col}=?")
                args.append(val)
        if states:
            where.append(f"state IN ({','.join('?' * len(states))})")
            args += list(states)
        sql = "SELECT * FROM operations" + (" WHERE " + " AND ".join(where) if where else "")
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY created_at, id LIMIT ?", [*args, limit]).fetchall()
        return [_op(r) for r in rows]

    def claim_next(self, lane: str, instance_id: str, prefer_affinity: str | None) -> Operation | None:
        """FIFO within a lane, but keep the loaded model's affinity while such work is queued (bounded by caller)."""
        with self._lock:
            row = None
            if prefer_affinity is not None:
                row = self._db.execute(
                    "SELECT * FROM operations WHERE lane=? AND state='queued' AND affinity=? ORDER BY created_at, id "
                    "LIMIT 1", (lane, prefer_affinity)).fetchone()
            if row is None:
                row = self._db.execute("SELECT * FROM operations WHERE lane=? AND state='queued' "
                                       "ORDER BY created_at, id LIMIT 1", (lane,)).fetchone()
            if row is None:
                return None
            now = now_iso()
            self._db.execute("UPDATE operations SET state='running', claimed_by=?, heartbeat_at=?, updated_at=?, "
                             "attempts=attempts+1 WHERE id=? AND state='queued'", (instance_id, now, now, row["id"]))
            self.changed.notify_all()
            return self.get(row["id"])

    def update(self, op_id: str, *, progress: dict[str, Any] | None = None, engine: dict[str, Any] | None = None,
               state: str | None = None) -> None:
        sets, args = ["updated_at=?", "heartbeat_at=?"], [now_iso(), now_iso()]
        if progress is not None:
            sets.append("progress=?")
            args.append(json.dumps(progress))
        if engine is not None:
            sets.append("engine=?")
            args.append(json.dumps(engine))
        if state is not None:
            sets.append("state=?")
            args.append(state)
        with self._lock:
            self._db.execute(f"UPDATE operations SET {', '.join(sets)} WHERE id=?", [*args, op_id])
            self.changed.notify_all()

    def finish(self, op_id: str, state: str, *, result: dict[str, Any] | None = None,
               error: dict[str, Any] | None = None) -> None:
        """A requested cancellation wins over a non-terminal outcome (blocked): it is never lost."""
        assert state in TERMINAL_STATES or state == "blocked"
        with self._lock:
            if state == "blocked" and (cur := self.get(op_id)) is not None and cur.state == "cancel_requested":
                state, error = "cancelled", {"code": "cancelled", "message": "cancelled while blocked"}
            self._db.execute("UPDATE operations SET state=?, result=?, error=?, updated_at=? WHERE id=?",
                             (state, json.dumps(result) if result is not None else None,
                              json.dumps(error) if error is not None else None, now_iso(), op_id))
            self.changed.notify_all()

    def request_cancel(self, op_id: str) -> Operation | None:
        with self._lock:
            op = self.get(op_id)
            if op is None:
                return None
            if op.state in ("held", "queued", "blocked"):
                self.finish(op_id, "cancelled", error={"code": "cancelled", "message": "cancelled before start"})
            elif op.state in ("running", "reconciling"):
                self.update(op_id, state="cancel_requested")
            return self.get(op_id)

    def requeue(self, op_id: str, from_states: tuple[str, ...] = ("blocked", "reconciling", "failed")) -> bool:
        """Conditional: a cancelled/cancel-requested op is never resurrected by a retry or restart race."""
        with self._lock:
            n = self._db.execute(
                f"UPDATE operations SET state='queued', error=NULL, updated_at=? WHERE id=? "
                f"AND state IN ({','.join('?' * len(from_states))})", (now_iso(), op_id, *from_states)).rowcount
            self.changed.notify_all()
            return n == 1

    def mark_running_as_reconciling(self) -> list[Operation]:
        """Startup: never assume work stopped or finished just because this process restarted. A pending
        cancellation is kept as intent (the op finishes as cancelled), never turned back into runnable work."""
        with self._lock:
            rows = self._db.execute("SELECT id, state FROM operations WHERE state IN ('running','cancel_requested')"
                                    ).fetchall()
            for r in rows:
                if r["state"] == "cancel_requested":
                    self.finish(r["id"], "cancelled", error={"code": "cancelled",
                                                             "message": "cancelled (confirmed after restart)"})
                else:
                    self._db.execute("UPDATE operations SET state='reconciling', updated_at=? WHERE id=?",
                                     (now_iso(), r["id"]))
        return [op for r in rows if r["state"] == "running" and (op := self.get(r["id"])) is not None]

    def next_epoch(self, lane: str) -> int:
        with self._lock:
            self._db.execute("INSERT INTO lane_epochs VALUES (?, 1) ON CONFLICT(lane) DO UPDATE SET epoch=epoch+1",
                             (lane,))
            return int(self._db.execute("SELECT epoch FROM lane_epochs WHERE lane=?", (lane,)).fetchone()[0])

    # --- command idempotency (synchronous commands such as approvals) -------------------------------------------
    def command_result(self, project_id: str, action: str, key: str,
                       payload: dict[str, Any]) -> dict[str, Any] | None:
        """Same scope + key + request -> the recorded response; same scope + key + other request -> conflict."""
        phash = sha256_json(payload)
        with self._lock:
            row = self._db.execute("SELECT * FROM scoped_commands WHERE project_id=? AND action=? AND key=?",
                                   (project_id, action, key)).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != phash:
            raise IdempotencyConflict("idempotency key reused with a different request")
        return json.loads(row["response"])

    def record_command(self, project_id: str, action: str, key: str, payload: dict[str, Any],
                       response: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO scoped_commands VALUES (?,?,?,?,?,?)",
                             (project_id, action, key, sha256_json(payload), json.dumps(response), now_iso()))
