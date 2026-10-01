"""Persistent server identity: created once, never derived from the (random per start) Settings.instance_id."""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..secure_files import create_private_exclusive, write_private

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class IdentityError(Exception):
    pass


@dataclass(frozen=True)
class ServerIdentity:
    server_id: str
    created_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _check(server_id: object) -> str:
    if not isinstance(server_id, str) or not _UUID.fullmatch(server_id):
        raise IdentityError("server_id must be a lowercase canonical UUID")
    return server_id


def _read(path: Path) -> ServerIdentity:
    try:
        raw = json.loads(path.read_text())
        created = raw["created_at"]
        if not isinstance(created, str):
            raise TypeError("created_at")
        return ServerIdentity(_check(raw["server_id"]), created)
    except (ValueError, KeyError, TypeError, IdentityError) as e:
        # Regenerating would silently fork the identity clients pinned; the operator must decide.
        raise IdentityError(f"{path} is corrupt ({type(e).__name__}); restore it or run "
                            "`assetstudio integration identity adopt`") from e


def load_or_create(path: Path) -> ServerIdentity:
    if path.exists():
        return _read(path)
    fresh = ServerIdentity(str(uuid.uuid4()), _now())
    if create_private_exclusive(path, asdict(fresh)):
        return fresh
    return _read(path)  # lost the creation race to another process


def adopt(path: Path, server_id: str, *, force: bool) -> ServerIdentity:
    _check(server_id)
    if path.exists() and not force:
        raise IdentityError("a server identity already exists; adopting replaces it (--i-understand-fork)")
    ident = ServerIdentity(server_id, _now())
    write_private(path, asdict(ident))
    return ident
