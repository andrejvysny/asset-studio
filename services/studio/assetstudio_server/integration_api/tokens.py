"""Library-scoped client tokens for the integration listener. Only sha256 hashes are stored (0600, atomic replace).

The CLI writes the file from another process; the store reloads whenever (mtime, size, inode) changes, so revocation
is visible on the next request. verify() never writes (a stale rewrite could resurrect a revoked token).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from assetstudio_core.ids import InvalidId, validate_id

from ..secure_files import write_private

TOKEN_PREFIX = "asi_"
NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
VALID_SCOPES = frozenset({"assets:read", "assets:publish"})


@dataclass
class ClientToken:
    name: str
    sha256: str
    scopes: list[str]
    library_ids: list[str]
    created_at: str
    revoked_at: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize(name: str, scopes: list[str], library_ids: list[str]) -> tuple[list[str], list[str]]:
    if not NAME.fullmatch(name):
        raise ValueError("token name: lowercase letters, digits, '_', '.', '-' (max 64)")
    if not scopes or not set(scopes) <= VALID_SCOPES:
        raise ValueError(f"scopes must be a non-empty subset of {sorted(VALID_SCOPES)}")
    if not library_ids:
        raise ValueError("at least one library id is required")
    try:
        libs = sorted({validate_id(v, "prj") for v in library_ids})
    except InvalidId as e:
        raise ValueError(str(e)) from e
    return sorted(set(scopes)), libs


class IntegrationTokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._tokens: list[ClientToken] = []
        self._sig: tuple[int, int, int] | None = None

    def _load(self) -> None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._tokens, self._sig = [], None
            return
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        if sig == self._sig:
            return
        raw = json.loads(self.path.read_text())
        self._tokens = [ClientToken(**t) for t in raw.get("tokens", [])]
        self._sig = sig

    def _save(self) -> None:
        write_private(self.path, {"tokens": [asdict(t) for t in self._tokens]})
        st = self.path.stat()
        self._sig = (st.st_mtime_ns, st.st_size, st.st_ino)

    def create(self, name: str, scopes: list[str], library_ids: list[str]) -> str:
        """Returns the plaintext token; it is never stored or shown again."""
        scopes, libs = normalize(name, scopes, library_ids)
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            self._load()
            if any(t.name == name and t.revoked_at is None for t in self._tokens):
                raise ValueError(f"token {name!r} already exists; revoke it first")
            self._tokens.append(ClientToken(name, _digest(token), scopes, libs, _now()))
            self._save()
        return token

    def revoke(self, name: str) -> bool:
        with self._lock:
            self._load()
            live = next((t for t in self._tokens if t.name == name and t.revoked_at is None), None)
            if live is None:
                return False
            live.revoked_at = _now()  # tombstone: the audit trail keeps who was revoked when
            self._save()
            return True

    def list(self) -> list[dict[str, object]]:
        with self._lock:
            self._load()
            return [{k: v for k, v in asdict(t).items() if k != "sha256"} for t in self._tokens]

    def verify(self, token: str) -> ClientToken | None:
        if not token.startswith(TOKEN_PREFIX):
            return None
        digest = _digest(token)
        with self._lock:
            self._load()
            found: ClientToken | None = None
            for t in self._tokens:  # no early exit: constant work per request
                if t.revoked_at is None and hmac.compare_digest(t.sha256, digest):
                    found = t
            return found
