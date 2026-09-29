"""Command envelope for side-effecting, replayable commands.

    validate + plan (no writes) -> durable intent (journal) -> idempotent effects -> recorded response

Effects are idempotent (derived ids, logical task keys, guarded item mutations), so a crash anywhere after the
intent is repaired by replaying the plan (at startup, or when the client retries with the same key).
Keys are scoped by project + action; the request hash covers the whole request including its target ids.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from assetstudio_core.ids import derived_id

from ..errors import ApiError
from ..journal import IdempotencyConflict
from ..registry import ProjectContext
from ..studio import Studio

log = logging.getLogger("assetstudio.commands")
Effects = Callable[[Studio, ProjectContext, dict[str, Any], str], dict[str, Any]]
REPLAY: dict[str, Effects] = {}


def replayable(action: str) -> Callable[[Effects], Effects]:
    def deco(fn: Effects) -> Effects:
        REPLAY[action] = fn
        return fn
    return deco


def command_id(ctx: ProjectContext, action: str, key: str) -> str:
    return derived_id("cmd", ctx.id, action, key)


def execute(studio: Studio, ctx: ProjectContext, action: str, key: str, request: dict[str, Any],
            plan: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    """Run `plan(command_id)` once, then the registered effects for `action`. Replays return the same response."""
    ctx.require_writable()
    journal = studio.journal
    try:
        prior = journal.command_result(ctx.id, action, key, request)
    except IdempotencyConflict as e:
        raise ApiError(409, e.code, str(e)) from e
    if prior is not None:
        return prior
    cid = command_id(ctx, action, key)
    existing = journal.tasks.intent(ctx.id, action, key)
    if existing is None:
        p = plan(cid)
        with journal.tasks.txn() as db:
            journal.tasks.record_intent(ctx.id, action, key, cid, {"request": request, "plan": p}, db)
    else:
        if existing["intent"]["request"] != request:
            raise ApiError(409, "idempotency_conflict", "idempotency key reused with a different request")
        p = existing["intent"]["plan"]
    response = REPLAY[action](studio, ctx, p, cid)
    journal.record_command(ctx.id, action, key, request, response)
    with journal.tasks.txn() as db:
        journal.tasks.complete_intent(ctx.id, action, key, db)
    return response


def replay_open_intents(studio: Studio) -> None:
    for row in studio.journal.tasks.open_intents():
        fn = REPLAY.get(row["action"])
        if fn is None:
            continue
        try:
            ctx = studio.registry.get(row["project_id"])
            response = fn(studio, ctx, row["intent"]["plan"], row["command_id"])
            studio.journal.record_command(row["project_id"], row["action"], row["key"], row["intent"]["request"],
                                          response)
            studio.journal.tasks.complete_intent(row["project_id"], row["action"], row["key"])
            log.warning("replayed unfinished command %s %s", row["action"], row["command_id"])
        except Exception:
            log.exception("replaying command %s failed; it stays pending", row["command_id"])
