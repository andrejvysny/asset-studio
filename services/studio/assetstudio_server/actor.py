"""Who issued the current request: the browser operator, or an MCP agent acting through the in-process loopback.

The actor header is honoured only together with a per-process secret that only the in-process MCP client knows, so a
browser or any other HTTP caller can never claim to be someone else. Commands persist the actor in their planned
intent, which keeps it across a crash replay.
"""
from __future__ import annotations

import hmac
import re
import secrets
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Literal

OPERATOR = "operator"
ACTOR_HEADER = "x-assetstudio-actor"
INTERNAL_HEADER = "x-assetstudio-internal"
SCOPE_HEADER = "x-assetstudio-agent-scope"
INTERNAL_SECRET = secrets.token_urlsafe(32)
_AGENT = re.compile(r"agent:[a-z0-9][a-z0-9_.-]{0,63}")
_current: ContextVar[str] = ContextVar("assetstudio_actor", default=OPERATOR)


def current() -> str:
    return _current.get()


def from_headers(actor: str | None, secret: str | None) -> str:
    if actor and secret and hmac.compare_digest(secret, INTERNAL_SECRET) and _AGENT.fullmatch(actor):
        return actor
    return OPERATOR


def internal_principal(headers: Mapping[str, str]) -> tuple[str, Literal["read", "full"]] | None:
    """(actor, scope) of the in-process MCP loopback; None for anything forged or partial (falls to normal auth)."""
    secret, who, scope = headers.get(INTERNAL_HEADER), headers.get(ACTOR_HEADER), headers.get(SCOPE_HEADER)
    if not (secret and who and scope in ("read", "full")) or not hmac.compare_digest(secret, INTERNAL_SECRET):
        return None
    return (who, scope) if _AGENT.fullmatch(who) else None  # type: ignore[return-value]


def set_current(actor: str) -> object:
    return _current.set(actor)


def reset(token: object) -> None:
    _current.reset(token)  # type: ignore[arg-type]
