"""Runner sessions, device claims and slots in the durable journal (tables come from journal migration v4).

Persistence primitives only: every state change is a conditional UPDATE so races resolve in SQL, not in callers.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.ids import new_id


@contextmanager
def journal_txn(db: sqlite3.Connection, lock: threading.RLock,
                changed: threading.Condition) -> Iterator[sqlite3.Connection]:
    with lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        changed.notify_all()


class DeviceConflict(Exception):
    """A physical device (by UUID) is already claimed by a different runner."""

    code = "forbidden_scope"

    def __init__(self, uuid: str, owner: str) -> None:
        super().__init__(f"device {uuid} already belongs to runner {owner}")
        self.uuid, self.owner = uuid, owner


def _session(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for k in ("platform", "software"):
        d[k] = json.loads(d[k])
    d["inventory"] = json.loads(d["inventory"]) if d["inventory"] else None
    return d


def _slot(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["device_uuids"] = json.loads(d["device_uuids"])
    d["engines"] = json.loads(d["engines"])
    return d


class RunnerStore:
    def __init__(self, db: sqlite3.Connection, lock: threading.RLock, changed: threading.Condition) -> None:
        self._db, self._lock, self.changed = db, lock, changed

    def txn(self) -> Any:
        return journal_txn(self._db, self._lock, self.changed)

    # --- sessions ------------------------------------------------------------------------------------------------
    def open_session(self, runner_id: str, boot_id: str, protocol_version: int, dispatch: str,
                     platform: dict[str, Any], software: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        with self.txn() as db:
            row = db.execute("SELECT * FROM runner_sessions WHERE runner_id=? AND boot_id=?",
                             (runner_id, boot_id)).fetchone()
            if row is not None:
                return _session(row), False
            now = now_iso()
            db.execute("UPDATE runner_sessions SET state='superseded' WHERE runner_id=? AND state='active'",
                       (runner_id,))
            sid = new_id("rse")
            db.execute(
                "INSERT INTO runner_sessions(id, runner_id, boot_id, protocol_version, dispatch, platform, software,"
                " state, created_at, last_seen_at) VALUES (?,?,?,?,?,?,?,'active',?,?)",
                (sid, runner_id, boot_id, protocol_version, dispatch, json.dumps(platform), json.dumps(software),
                 now, now))
            return _session(db.execute("SELECT * FROM runner_sessions WHERE id=?", (sid,)).fetchone()), True

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM runner_sessions WHERE id=?", (session_id,)).fetchone()
        return _session(row) if row else None

    def active_session(self, runner_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM runner_sessions WHERE runner_id=? AND state='active'",
                                   (runner_id,)).fetchone()
        return _session(row) if row else None

    def touch_session(self, session_id: str, lifecycle: str | None = None) -> bool:
        with self._lock:
            n = self._db.execute(
                "UPDATE runner_sessions SET last_seen_at=?, lifecycle=COALESCE(?, lifecycle) "
                "WHERE id=? AND state='active'", (now_iso(), lifecycle, session_id)).rowcount
            self.changed.notify_all()
        return n == 1

    def set_inventory(self, session_id: str, revision: int, inventory: dict[str, Any]) -> bool:
        with self._lock:
            n = self._db.execute(
                "UPDATE runner_sessions SET inventory=?, inventory_revision=? "
                "WHERE id=? AND state='active' AND inventory_revision < ?",
                (json.dumps(inventory), revision, session_id, revision)).rowcount
            self.changed.notify_all()
        return n == 1

    def sessions(self, runner_id: str | None = None,
                 states: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        if runner_id is not None:
            where.append("runner_id=?")
            args.append(runner_id)
        if states is not None:
            where.append(f"state IN ({','.join('?' * len(states))})")
            args.extend(states)
        sql = "SELECT * FROM runner_sessions" + (" WHERE " + " AND ".join(where) if where else "")
        with self._lock:
            return [_session(r) for r in self._db.execute(sql + " ORDER BY created_at, id", args)]

    # --- devices -------------------------------------------------------------------------------------------------
    def claim_devices(self, runner_id: str, devices: list[dict[str, Any]]) -> None:
        with self.txn() as db:
            for d in devices:
                row = db.execute("SELECT runner_id, claim FROM devices WHERE uuid=?", (d["uuid"],)).fetchone()
                if row is not None and row["runner_id"] != runner_id and row["claim"] != "retired":
                    raise DeviceConflict(d["uuid"], row["runner_id"])
            now = now_iso()
            for d in devices:
                db.execute(
                    "INSERT INTO devices(uuid, runner_id, idx, name, memory_mb, fallback, updated_at) "
                    "VALUES (?,?,?,?,?,?,?) ON CONFLICT(uuid) DO UPDATE SET runner_id=excluded.runner_id, "
                    "idx=excluded.idx, name=excluded.name, memory_mb=excluded.memory_mb, "
                    "fallback=excluded.fallback, updated_at=excluded.updated_at",
                    (d["uuid"], runner_id, d["index"], d["name"], d["memory_mb"], int(bool(d["fallback"])), now))

    def reserve_device(self, uuid: str, attempt_id: str) -> bool:
        with self._lock:
            n = self._db.execute(
                "UPDATE devices SET claim='reserved', claim_attempt=?, updated_at=? WHERE uuid=? AND claim='free'",
                (attempt_id, now_iso(), uuid)).rowcount
            self.changed.notify_all()
        return n == 1

    def release_device(self, uuid: str, attempt_id: str) -> bool:
        with self._lock:
            n = self._db.execute(
                "UPDATE devices SET claim='free', claim_attempt=NULL, updated_at=? WHERE uuid=? "
                "AND claim IN ('reserved','uncertain') AND claim_attempt=?", (now_iso(), uuid, attempt_id)).rowcount
            self.changed.notify_all()
        return n == 1

    def mark_device_uncertain(self, uuid: str) -> bool:
        with self._lock:
            n = self._db.execute("UPDATE devices SET claim='uncertain', updated_at=? WHERE uuid=? AND claim='reserved'",
                                 (now_iso(), uuid)).rowcount
            self.changed.notify_all()
        return n == 1

    def retire_device(self, uuid: str) -> bool:
        with self._lock:
            n = self._db.execute("UPDATE devices SET claim='retired', claim_attempt=NULL, updated_at=? WHERE uuid=? "
                                 "AND claim != 'retired'", (now_iso(), uuid)).rowcount
            self.changed.notify_all()
        return n == 1

    def devices(self, runner_id: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM devices", []
        if runner_id is not None:
            sql, args = sql + " WHERE runner_id=?", [runner_id]
        with self._lock:
            return [{**dict(r), "fallback": bool(r["fallback"])}
                    for r in self._db.execute(sql + " ORDER BY runner_id, idx, uuid", args)]

    # --- slots ---------------------------------------------------------------------------------------------------
    def replace_slots(self, runner_id: str, slots: list[dict[str, Any]]) -> None:
        now = now_iso()
        with self.txn() as db:
            db.execute("DELETE FROM slots WHERE runner_id=?", (runner_id,))
            for s in slots:
                db.execute("INSERT INTO slots VALUES (?,?,?,?,?,?,?,?)",
                           (runner_id, s["slot_id"], s["capability"], json.dumps(s["device_uuids"]),
                            json.dumps(s["engines"]), s.get("loaded_residency"), s["state"], now))

    def slots(self, runner_id: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM slots", []
        if runner_id is not None:
            sql, args = sql + " WHERE runner_id=?", [runner_id]
        with self._lock:
            return [_slot(r) for r in self._db.execute(sql + " ORDER BY runner_id, slot_id", args)]
