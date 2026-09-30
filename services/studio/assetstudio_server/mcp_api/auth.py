"""Bearer tokens for the MCP listener. Only sha256 hashes are stored (instance dir, 0600, atomic replace).

The CLI edits the file while the Studio runs; the store reloads it when its mtime changes, so revocation is immediate.
The server never rewrites the token file (a stale rewrite could resurrect a revoked token): usage times go to a
separate `<name>.used.json` that only the server writes.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

Scope_ = Literal["read", "full"]
TOKEN_PREFIX = "ast_"
NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
STATE_KEY = "mcp_token"
LAST_USED_WRITE_S = 300  # last_used is informational; persisting it at most every 5 min keeps verify cheap


@dataclass
class TokenInfo:
    name: str
    scope: Scope_
    sha256: str
    created_at: str
    last_used_at: str | None = None

    @property
    def actor(self) -> str:
        return f"agent:{self.name}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._tokens: dict[str, TokenInfo] = {}
        self._mtime: float | None = None
        self._used_path = path.with_suffix(".used.json")
        self._used: dict[str, str] = {}
        self._used_written = 0.0

    def _load(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._tokens, self._mtime = {}, None
            return
        if mtime == self._mtime:
            return
        raw = json.loads(self.path.read_text())
        self._tokens = {t["name"]: TokenInfo(**t) for t in raw.get("tokens", [])}
        self._mtime = mtime

    def _save(self) -> None:
        _write_private(self.path, {"tokens": [asdict(t) for t in self._tokens.values()]})
        self._mtime = self.path.stat().st_mtime

    def create(self, name: str, scope: Scope_) -> str:
        """Returns the plaintext token; it is never stored or shown again."""
        if not NAME.fullmatch(name):
            raise ValueError("token name: lowercase letters, digits, '_', '.', '-' (max 64)")
        if scope not in ("read", "full"):
            raise ValueError("scope must be 'read' or 'full'")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            self._load()
            if name in self._tokens:
                raise ValueError(f"token {name!r} already exists; revoke it first")
            self._tokens[name] = TokenInfo(name=name, scope=scope, sha256=_digest(token), created_at=_now())
            self._save()
        return token

    def revoke(self, name: str) -> bool:
        with self._lock:
            self._load()
            if self._tokens.pop(name, None) is None:
                return False
            self._save()
            return True

    def list(self) -> list[dict[str, str | None]]:
        with self._lock:
            self._load()
            try:
                used = json.loads(self._used_path.read_text())
            except (FileNotFoundError, ValueError):
                used = {}
            used |= self._used
            return [{k: v for k, v in asdict(t).items() if k != "sha256"} | {"last_used_at": used.get(t.name)}
                    for t in self._tokens.values()]

    def verify(self, token: str) -> TokenInfo | None:
        if not token.startswith(TOKEN_PREFIX):
            return None
        digest = _digest(token)
        with self._lock:
            self._load()
            found = next((t for t in self._tokens.values() if hmac.compare_digest(t.sha256, digest)), None)
            if found is not None:
                self._touch(found)
            return found

    def _touch(self, t: TokenInfo) -> None:
        self._used[t.name] = t.last_used_at = _now()
        now = datetime.now(UTC).timestamp()
        if now - self._used_written >= LAST_USED_WRITE_S:
            self._used_written = now
            _write_private(self._used_path, self._used)


def _write_private(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class BearerAuth:
    """Every HTTP request needs a valid token, except `open_prefixes` (signed file URLs carry their own proof)."""

    def __init__(self, app: ASGIApp, store: TokenStore, open_prefixes: tuple[str, ...] = ()) -> None:
        self.app, self.store, self.open_prefixes = app, store, open_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(self.open_prefixes):
            await self.app(scope, receive, send)
            return
        header = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
        scheme, _, token = header.partition(" ")
        info = self.store.verify(token.strip()) if scheme.lower() == "bearer" else None
        if info is None:
            response = JSONResponse({"error": {"code": "unauthorized", "message": "valid bearer token required"}},
                                    status_code=401, headers={"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})[STATE_KEY] = info
        await self.app(scope, receive, send)
