"""Execution attempts, their event trail and resumable uploads in the durable journal (tables: migration v4).

An attempt is one (task, call, generation) offer to a runner. Conditional transitions make offer/lease/cancel/report
races safe; `control` (cancel intent) is separate from execution state, like StageTask.control.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.ids import derived_id, new_id
from assetstudio_protocol.execution import TERMINAL_STATES

from .runnerstore import journal_txn

_JSON_COLS = ("offer", "manifest", "error", "progress")
_TRANSITION_COLS = frozenset({"runner_id", "session_id", "slot_id", "lease_until", "offer_expires_at", "manifest",
                              "error", "progress", "disposition", "disposition_at", "offer"})
_LIVE_UPLOAD = ("open", "finalizing")


class StaleRevision(Exception):
    """The call key was already offered with different inputs: the caller's view of the task is stale."""

    code = "stale_revision"


def _attempt(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for k in _JSON_COLS:
        d[k] = json.loads(d[k]) if d[k] is not None else None
    return d


def _upload(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["received"] = json.loads(d["received"])
    return d


def _in(values: tuple[str, ...]) -> str:
    return ",".join("?" * len(values))


class AttemptStore:
    def __init__(self, db: sqlite3.Connection, lock: threading.RLock, changed: threading.Condition) -> None:
        self._db, self._lock, self.changed = db, lock, changed

    def txn(self) -> Any:
        return journal_txn(self._db, self._lock, self.changed)

    @staticmethod
    def attempt_id_for(task_id: str, call_key: str, generation: int) -> str:
        return derived_id("atp", task_id, call_key, str(generation))

    @staticmethod
    def _event(db: sqlite3.Connection, attempt_id: str, event: str, detail: dict[str, Any] | None) -> None:
        db.execute("INSERT INTO attempt_events(attempt_id, at, event, detail) VALUES (?,?,?,?)",
                   (attempt_id, now_iso(), event, json.dumps(detail or {})))

    # --- attempts ------------------------------------------------------------------------------------------------
    def create_offer(self, *, task_id: str, call_key: str, generation: int, project_id: str, operation: str,
                     operation_version: int, input_digest: str, offer: dict[str, Any], runner_id: str | None,
                     session_id: str | None, slot_id: str | None,
                     offer_expires_at: str | None) -> tuple[dict[str, Any], bool]:
        aid = self.attempt_id_for(task_id, call_key, generation)
        with self.txn() as db:
            if db.execute("SELECT 1 FROM attempts WHERE task_id=? AND call_key=? AND input_digest != ? LIMIT 1",
                          (task_id, call_key, input_digest)).fetchone():
                raise StaleRevision(f"{task_id}/{call_key} was offered with different inputs")
            row = db.execute("SELECT * FROM attempts WHERE id=?", (aid,)).fetchone()
            if row is not None:
                return _attempt(row), False
            now = now_iso()
            db.execute(
                "INSERT INTO attempts(id, task_id, call_key, generation, project_id, operation, operation_version,"
                " input_digest, offer, runner_id, session_id, slot_id, state, offer_expires_at, created_at,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'offered',?,?,?)",
                (aid, task_id, call_key, generation, project_id, operation, operation_version, input_digest,
                 json.dumps(offer), runner_id, session_id, slot_id, offer_expires_at, now, now))
            self._event(db, aid, "offered", None)
            return _attempt(db.execute("SELECT * FROM attempts WHERE id=?", (aid,)).fetchone()), True

    def get(self, attempt_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        return _attempt(row) if row else None

    def for_call(self, task_id: str, call_key: str) -> list[dict[str, Any]]:
        with self._lock:
            return [_attempt(r) for r in self._db.execute(
                "SELECT * FROM attempts WHERE task_id=? AND call_key=? ORDER BY generation", (task_id, call_key))]

    def latest(self, task_id: str, call_key: str) -> dict[str, Any] | None:
        rows = self.for_call(task_id, call_key)
        return rows[-1] if rows else None

    def list(self, *, states: tuple[str, ...] | None = None, runner_id: str | None = None,
             project_id: str | None = None, task_id: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        where, args = [], []
        if states is not None:
            where.append(f"state IN ({_in(states)})")
            args.extend(states)
        for col, val in (("runner_id", runner_id), ("project_id", project_id), ("task_id", task_id)):
            if val is not None:
                where.append(f"{col}=?")
                args.append(val)
        sql = "SELECT * FROM attempts" + (" WHERE " + " AND ".join(where) if where else "")
        with self._lock:
            return [_attempt(r) for r in self._db.execute(sql + " ORDER BY created_at, id LIMIT ?", (*args, limit))]

    def transition(self, attempt_id: str, from_states: tuple[str, ...], state: str, *, event: str | None = None,
                   detail: dict[str, Any] | None = None, **cols: Any) -> bool:
        if (bad := set(cols) - _TRANSITION_COLS):
            raise ValueError(f"unknown attempt columns: {sorted(bad)}")
        sets, args = ["state=?", "updated_at=?", "revision=revision+1"], [state, now_iso()]
        for k, v in cols.items():
            sets.append(f"{k}=?")
            args.append(json.dumps(v) if k in _JSON_COLS and v is not None else v)
        with self.txn() as db:
            n = db.execute(f"UPDATE attempts SET {', '.join(sets)} WHERE id=? AND state IN ({_in(from_states)})",
                           [*args, attempt_id, *from_states]).rowcount
            if n == 1:
                self._event(db, attempt_id, event or state, detail)
        return n == 1

    def set_control(self, attempt_id: str, control: str) -> bool:
        if control not in ("run", "cancel"):
            raise ValueError(f"invalid control {control!r}")
        terminal = tuple(sorted(TERMINAL_STATES))
        with self._lock:
            n = self._db.execute(
                f"UPDATE attempts SET control=?, updated_at=?, revision=revision+1 "
                f"WHERE id=? AND state NOT IN ({_in(terminal)})", (control, now_iso(), attempt_id, *terminal)).rowcount
            self.changed.notify_all()
        return n == 1

    def set_disposition(self, attempt_id: str, disposition: str) -> bool:
        now = now_iso()
        with self.txn() as db:
            n = db.execute("UPDATE attempts SET disposition=?, disposition_at=?, updated_at=?, revision=revision+1 "
                           "WHERE id=? AND disposition IS NULL", (disposition, now, now, attempt_id)).rowcount
            if n == 1:
                self._event(db, attempt_id, "disposition", {"disposition": disposition})
        return n == 1

    def pending_receipts(self, runner_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [_attempt(r) for r in self._db.execute(
                "SELECT * FROM attempts WHERE runner_id=? AND disposition IS NOT NULL AND receipt_delivered=0 "
                "ORDER BY disposition_at, id", (runner_id,))]

    def mark_receipts_delivered(self, ids: list[str]) -> int:
        if not ids:
            return 0
        with self._lock:
            n = self._db.execute(f"UPDATE attempts SET receipt_delivered=1 WHERE id IN ({_in(tuple(ids))})",
                                 ids).rowcount
            self.changed.notify_all()
        return n

    def events(self, attempt_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [{**dict(r), "detail": json.loads(r["detail"])} for r in self._db.execute(
                "SELECT * FROM attempt_events WHERE attempt_id=? ORDER BY seq", (attempt_id,))]

    # --- uploads -------------------------------------------------------------------------------------------------
    def create_upload(self, *, attempt_id: str, generation: int, project_id: str, runner_id: str, sha256: str,
                      size: int, role: str, mime: str, chunk_size: int,
                      expires_at: str) -> tuple[dict[str, Any], bool]:
        with self.txn() as db:
            row = db.execute("SELECT * FROM uploads WHERE attempt_id=? AND generation=? AND sha256=?",
                             (attempt_id, generation, sha256)).fetchone()
            if row is not None and row["state"] != "expired":
                return _upload(row), False
            if row is not None:  # an expired session (abandoned or failed verification) must not block a retry
                db.execute("DELETE FROM uploads WHERE id=?", (row["id"],))
            uid = new_id("xfr")
            db.execute(
                "INSERT INTO uploads(id, attempt_id, generation, project_id, runner_id, sha256, size, role, mime,"
                " chunk_size, state, reserved_bytes, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,'open',?,?,?)",
                (uid, attempt_id, generation, project_id, runner_id, sha256, size, role, mime, chunk_size, size,
                 now_iso(), expires_at))
            return _upload(db.execute("SELECT * FROM uploads WHERE id=?", (uid,)).fetchone()), True

    def find_upload(self, attempt_id: str, generation: int, sha256: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM uploads WHERE attempt_id=? AND generation=? AND sha256=?",
                                   (attempt_id, generation, sha256)).fetchone()
        return _upload(row) if row else None

    def get_upload(self, upload_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
        return _upload(row) if row else None

    def record_chunk(self, upload_id: str, index: int, chunk_sha256: str) -> str:
        with self.txn() as db:
            row = db.execute("SELECT state, received FROM uploads WHERE id=?", (upload_id,)).fetchone()
            if row is None or row["state"] != "open":
                return "closed"
            received = json.loads(row["received"])
            known = received.get(str(index))
            if known is not None:
                return "duplicate" if known == chunk_sha256 else "conflict"
            received[str(index)] = chunk_sha256
            db.execute("UPDATE uploads SET received=? WHERE id=?", (json.dumps(received), upload_id))
            return "stored"

    def set_upload_state(self, upload_id: str, from_states: tuple[str, ...], state: str,
                         finalized_at: str | None = None) -> bool:
        release = ", reserved_bytes=0" if state in ("finalized", "expired") else ""
        with self._lock:
            n = self._db.execute(
                f"UPDATE uploads SET state=?, finalized_at=COALESCE(?, finalized_at){release} "
                f"WHERE id=? AND state IN ({_in(from_states)})",
                (state, finalized_at, upload_id, *from_states)).rowcount
            self.changed.notify_all()
        return n == 1

    def expired_uploads(self, now_iso_str: str) -> list[dict[str, Any]]:
        with self._lock:
            return [_upload(r) for r in self._db.execute(
                f"SELECT * FROM uploads WHERE state IN ({_in(_LIVE_UPLOAD)}) AND expires_at < ? ORDER BY expires_at",
                (*_LIVE_UPLOAD, now_iso_str))]

    def reserved_bytes(self, *, project_id: str | None = None, runner_id: str | None = None) -> int:
        where, args = [f"state IN ({_in(_LIVE_UPLOAD)})"], list(_LIVE_UPLOAD)
        for col, val in (("project_id", project_id), ("runner_id", runner_id)):
            if val is not None:
                where.append(f"{col}=?")
                args.append(val)
        with self._lock:
            row = self._db.execute(f"SELECT COALESCE(SUM(reserved_bytes), 0) FROM uploads WHERE {' AND '.join(where)}",
                                   args).fetchone()
        return int(row[0])
