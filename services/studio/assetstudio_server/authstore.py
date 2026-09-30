"""Instance auth DB for compute runners: groups, registration tokens, runners, nonces, access tokens, audit.

Its own SQLite file (mode 0600), separate from the operations journal: credentials never share a file with
dispatch state. Bearer secrets are stored only as SHA-256 digests; the plaintext is returned once to the caller.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from assetstudio_core.canonical import sha256_bytes
from assetstudio_core.ids import new_id

SCHEMA_VERSION = 1
MAX_REGISTRATION_TTL_S = 3600

_DDL = """
CREATE TABLE IF NOT EXISTS runner_groups (
  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, projects TEXT NOT NULL, operations TEXT NOT NULL,
  labels TEXT NOT NULL, ephemeral INTEGER NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS registration_tokens (
  token_sha256 TEXT PRIMARY KEY, group_id TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT, used_by TEXT,
  created_at TEXT NOT NULL, created_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runners (
  id TEXT PRIMARY KEY, group_id TEXT NOT NULL, name TEXT NOT NULL, public_key TEXT NOT NULL UNIQUE,
  platform TEXT NOT NULL, state TEXT NOT NULL, push_url TEXT, created_at TEXT NOT NULL, revoked_at TEXT,
  last_seen_at TEXT);
CREATE TABLE IF NOT EXISTS nonces (
  nonce TEXT PRIMARY KEY, runner_id TEXT NOT NULL, audience TEXT NOT NULL, expires_at TEXT NOT NULL,
  consumed_at TEXT);
CREATE TABLE IF NOT EXISTS access_tokens (
  token_sha256 TEXT PRIMARY KEY, runner_id TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS studio_keys (
  key_id TEXT PRIMARY KEY, private_key BLOB NOT NULL, public_key TEXT NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL,
  runner_id TEXT, detail TEXT NOT NULL DEFAULT '{}');
"""


class AuthStoreTooNew(Exception):
    pass


class RegistrationRefused(Exception):
    """reason: unknown_token | expired | already_used | key_in_use"""

    def __init__(self, reason: str) -> None:
        super().__init__(f"registration refused: {reason}")
        self.reason = reason


class Unauthorized(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(f"unauthorized: {reason}")
        self.reason = reason


def _fmt(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _hash(token: str) -> str:
    return sha256_bytes(token.encode())


def _runner(row: sqlite3.Row) -> dict[str, Any]:
    return {**dict(row), "platform": json.loads(row["platform"])}


def _group(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for k in ("projects", "operations", "labels"):
        d[k] = json.loads(d[k])
    d["ephemeral"] = bool(d["ephemeral"])
    return d


class AuthStore:
    def __init__(self, path: Path, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        found = self._db.execute("PRAGMA user_version").fetchone()[0]
        if found > SCHEMA_VERSION:
            self._db.close()
            raise AuthStoreTooNew(f"auth schema v{found} is newer than this Studio (v{SCHEMA_VERSION})")
        self._db.executescript(_DDL)
        self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        os.chmod(path, 0o600)

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _stamp(self, plus_s: int = 0) -> str:
        return _fmt(self._now() + timedelta(seconds=plus_s))

    # --- groups --------------------------------------------------------------------------------------------------
    def create_group(self, name: str, projects: list[str] | Literal["*"], operations: list[str] | Literal["*"],
                     labels: list[str], ephemeral: bool, created_by: str) -> dict[str, Any]:
        gid = new_id("rgp")
        try:
            with self._txn() as db:
                db.execute("INSERT INTO runner_groups VALUES (?,?,?,?,?,?,?,?)",
                           (gid, name, json.dumps(projects), json.dumps(operations), json.dumps(labels),
                            int(ephemeral), self._stamp(), created_by))
        except sqlite3.IntegrityError as e:
            raise ValueError(f"runner group name {name!r} already exists") from e
        self.audit("group_create", created_by, detail={"group_id": gid, "name": name})
        return self.get_group(gid) or {}

    def get_group(self, group_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM runner_groups WHERE id=?", (group_id,)).fetchone()
        return _group(row) if row else None

    def groups(self) -> list[dict[str, Any]]:
        with self._lock:
            return [_group(r) for r in self._db.execute("SELECT * FROM runner_groups ORDER BY created_at, id")]

    # --- registration --------------------------------------------------------------------------------------------
    def create_registration_token(self, group_id: str, ttl_s: int, created_by: str) -> str:
        if not 0 < ttl_s <= MAX_REGISTRATION_TTL_S:
            raise ValueError(f"ttl_s must be 1..{MAX_REGISTRATION_TTL_S}")
        if self.get_group(group_id) is None:
            raise KeyError(group_id)
        token = secrets.token_urlsafe(32)
        with self._txn() as db:
            db.execute("INSERT INTO registration_tokens(token_sha256, group_id, expires_at, created_at, created_by)"
                       " VALUES (?,?,?,?,?)", (_hash(token), group_id, self._stamp(ttl_s), self._stamp(), created_by))
        self.audit("registration_token_create", created_by, detail={"group_id": group_id, "ttl_s": ttl_s})
        return token

    def register_runner(self, token: str, public_key: str, name: str, platform: dict[str, Any]) -> dict[str, Any]:
        runner, reason = self._register(token, public_key, name, platform)
        if runner is None:
            self.audit("register_refused", "anonymous", detail={"reason": reason, "name": name})
            raise RegistrationRefused(reason)
        self.audit("register", runner["id"], runner["id"], {"group_id": runner["group_id"], "reason": reason})
        return runner

    def _register(self, token: str, public_key: str, name: str,
                  platform: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        with self._txn() as db:
            tok = db.execute("SELECT * FROM registration_tokens WHERE token_sha256=?", (_hash(token),)).fetchone()
            existing = db.execute("SELECT * FROM runners WHERE public_key=?", (public_key,)).fetchone()
            if existing is not None and tok is not None and tok["used_by"] == existing["id"]:
                return _runner(existing), "retry"
            if tok is None:
                return None, "unknown_token"
            if tok["used_at"] is not None:
                return None, "already_used"
            if _parse(tok["expires_at"]) <= self._now():
                return None, "expired"
            if existing is not None:
                return None, "key_in_use"
            rid, now = new_id("rnr"), self._stamp()
            db.execute("INSERT INTO runners(id, group_id, name, public_key, platform, state, created_at) "
                       "VALUES (?,?,?,?,?,'active',?)", (rid, tok["group_id"], name, public_key,
                                                         json.dumps(platform), now))
            db.execute("UPDATE registration_tokens SET used_at=?, used_by=? WHERE token_sha256=?",
                       (now, rid, _hash(token)))
            return _runner(db.execute("SELECT * FROM runners WHERE id=?", (rid,)).fetchone()), "new"

    # --- runners -------------------------------------------------------------------------------------------------
    def get_runner(self, runner_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM runners WHERE id=?", (runner_id,)).fetchone()
        return _runner(row) if row else None

    def runners(self, group_id: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM runners", []
        if group_id is not None:
            sql, args = sql + " WHERE group_id=?", [group_id]
        with self._lock:
            return [_runner(r) for r in self._db.execute(sql + " ORDER BY created_at, id", args)]

    def set_push_url(self, runner_id: str, url: str | None) -> None:
        with self._txn() as db:
            db.execute("UPDATE runners SET push_url=? WHERE id=?", (url, runner_id))

    def touch_runner(self, runner_id: str) -> None:
        with self._txn() as db:
            db.execute("UPDATE runners SET last_seen_at=? WHERE id=?", (self._stamp(), runner_id))

    def revoke_runner(self, runner_id: str, actor: str) -> bool:
        with self._txn() as db:
            n = db.execute("UPDATE runners SET state='revoked', revoked_at=? WHERE id=? AND state='active'",
                           (self._stamp(), runner_id)).rowcount
            if n == 1:
                db.execute("DELETE FROM access_tokens WHERE runner_id=?", (runner_id,))
                db.execute("DELETE FROM nonces WHERE runner_id=? AND consumed_at IS NULL", (runner_id,))
        if n == 1:
            self.audit("revoke", actor, runner_id)
        return n == 1

    def _require_active(self, db: sqlite3.Connection, runner_id: str) -> None:
        row = db.execute("SELECT state FROM runners WHERE id=?", (runner_id,)).fetchone()
        if row is None:
            raise Unauthorized("unknown_runner")
        if row["state"] != "active":
            raise Unauthorized("revoked")

    # --- nonces + access tokens ----------------------------------------------------------------------------------
    def issue_nonce(self, runner_id: str, audience: str, ttl_s: int = 60,
                    max_outstanding: int = 8) -> tuple[str, str]:
        # Stored plaintext: single-use, short-lived and useless without the runner's signature over it.
        nonce, now, expires = secrets.token_urlsafe(32), self._stamp(), self._stamp(ttl_s)
        with self._txn() as db:
            self._require_active(db, runner_id)
            db.execute("DELETE FROM nonces WHERE runner_id=? AND expires_at <= ?", (runner_id, now))
            outstanding = db.execute("SELECT COUNT(*) FROM nonces WHERE runner_id=? AND consumed_at IS NULL",
                                     (runner_id,)).fetchone()[0]
            if outstanding >= max_outstanding:
                raise Unauthorized("too_many_nonces")
            db.execute("INSERT INTO nonces(nonce, runner_id, audience, expires_at) VALUES (?,?,?,?)",
                       (nonce, runner_id, audience, expires))
        return nonce, expires

    def consume_nonce(self, nonce: str, runner_id: str, audience: str) -> bool:
        with self._txn() as db:
            n = db.execute(
                "UPDATE nonces SET consumed_at=? WHERE nonce=? AND runner_id=? AND audience=? "
                "AND consumed_at IS NULL AND expires_at > ?",
                (self._stamp(), nonce, runner_id, audience, self._stamp())).rowcount
        return n == 1

    def issue_access_token(self, runner_id: str, ttl_s: int = 900) -> tuple[str, str]:
        token, expires = secrets.token_urlsafe(32), self._stamp(ttl_s)
        with self._txn() as db:
            self._require_active(db, runner_id)
            db.execute("INSERT INTO access_tokens VALUES (?,?,?,?)", (_hash(token), runner_id, expires, self._stamp()))
        return token, expires

    def resolve_access_token(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT r.*, t.expires_at AS token_expires_at FROM access_tokens t JOIN runners r ON r.id=t.runner_id "
                "WHERE t.token_sha256=? AND t.expires_at > ? AND r.state='active'",
                (_hash(token), self._stamp())).fetchone()
        return _runner(row) if row else None

    # --- audit ---------------------------------------------------------------------------------------------------
    def audit(self, event: str, actor: str, runner_id: str | None = None,
              detail: dict[str, Any] | None = None) -> None:
        with self._txn() as db:
            db.execute("INSERT INTO audit(at, actor, event, runner_id, detail) VALUES (?,?,?,?,?)",
                       (self._stamp(), actor, event, runner_id, json.dumps(detail or {})))

    def audit_log(self, limit: int = 200, runner_id: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM audit", []
        if runner_id is not None:
            sql, args = sql + " WHERE runner_id=?", [runner_id]
        with self._lock:
            return [{**dict(r), "detail": json.loads(r["detail"])}
                    for r in self._db.execute(sql + " ORDER BY seq DESC LIMIT ?", (*args, limit))]

    # --- studio signing keys (opaque bytes; crypto lives elsewhere) ----------------------------------------------
    def add_studio_key(self, key_id: str, private_key: bytes, public_key_b64: str, status: str) -> None:
        with self._txn() as db:
            db.execute("INSERT INTO studio_keys VALUES (?,?,?,?,?)",
                       (key_id, private_key, public_key_b64, status, self._stamp()))

    def studio_keys(self, include_private: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM studio_keys WHERE status IN ('current','next') "
                                    "ORDER BY created_at, key_id").fetchall()
        return [{k: v for k, v in dict(r).items() if include_private or k != "private_key"} for r in rows]

    def set_studio_key_status(self, key_id: str, status: str) -> bool:
        with self._txn() as db:
            return db.execute("UPDATE studio_keys SET status=? WHERE key_id=?", (status, key_id)).rowcount == 1
