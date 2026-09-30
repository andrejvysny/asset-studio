"""Who issued the current request: the browser operator, or an MCP agent acting through the in-process loopback.

The actor header is honoured only together with a per-process secret that only the in-process MCP client knows, so a
browser or any other HTTP caller can never claim to be someone else. Commands persist the actor in their planned
intent, which keeps it across a crash replay.
"""
from __future__ import annotations

import hmac
import re
import secrets
from contextvars import ContextVar

OPERATOR = "operator"
ACTOR_HEADER = "x-assetstudio-actor"
INTERNAL_HEADER = "x-assetstudio-internal"
INTERNAL_SECRET = secrets.token_urlsafe(32)
_AGENT = re.compile(r"agent:[a-z0-9][a-z0-9_.-]{0,63}")
_current: ContextVar[str] = ContextVar("assetstudio_actor", default=OPERATOR)


def current() -> str:
    return _current.get()


def from_headers(actor: str | None, secret: str | None) -> str:
    if actor and secret and hmac.compare_digest(secret, INTERNAL_SECRET) and _AGENT.fullmatch(actor):
        return actor
    return OPERATOR


def set_current(actor: str) -> object:
    return _current.set(actor)


def reset(token: object) -> None:
    _current.reset(token)  # type: ignore[arg-type]
