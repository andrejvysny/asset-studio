"""Long-poll change feed over the in-process EventBus: opaque cursors, per-token filtering, invalidation hints only.

`access_changed` is reserved and never emitted in v1 because integration tokens are immutable."""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ..events import EventBus
from .auth import principal
from .errors import IntegrationError
from .principal import Principal

router = APIRouter(prefix="/api/integration/v1")
MAX_EVENTS = 500
POLL_S = 0.25
ASSET_TYPES = {"published": "asset_current_changed", "current": "asset_current_changed",
               "metadata": "asset_metadata_changed", "delivery_ready": "delivery_ready"}


def encode_cursor(epoch: str, seq: int) -> str:
    raw = json.dumps({"e": epoch, "s": seq}, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode_cursor(cursor: str) -> tuple[str, int]:
    try:
        doc = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        epoch, seq = doc["e"], doc["s"]
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise invalid_cursor() from None
    if not isinstance(epoch, str) or isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise invalid_cursor()
    return epoch, seq


def invalid_cursor() -> IntegrationError:
    return IntegrationError(400, "invalid_request", "malformed cursor")


def project_event(event: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any] | None:
    """Public shape of one bus event for this principal, or None. Only ids ever leave: no names, no actors."""
    library_id = event.get("project_id")
    if event.get("type") != "library" or library_id not in allowed:
        return None
    asset_id = event.get("asset_id")
    kind = ASSET_TYPES.get(event.get("change", ""))
    if kind is None or not asset_id:
        return {"type": "library_changed", "library_id": library_id}
    return {"type": kind, "library_id": library_id, "asset_id": asset_id}


def _reset(bus: EventBus) -> dict[str, Any]:
    return {"cursor": encode_cursor(bus.epoch, bus.seq), "events": [], "reset_required": True}


def _collect(bus: EventBus, pos: int, allowed: frozenset[str]) -> tuple[int, list[dict[str, Any]]]:
    """Advance past every event (visible or not); stop at the cap so the cursor never skips unsent ones."""
    batch, _ = bus.since(pos, timeout=0)
    out: list[dict[str, Any]] = []
    for e in batch:
        if len(out) >= MAX_EVENTS:
            break
        pos = e["seq"]
        shown = project_event(e, allowed)
        if shown is not None:
            out.append(shown)
    return pos, out


@router.get("/changes")
async def changes(request: Request, cursor: str | None = None,
                  timeout_s: float = Query(20.0, ge=0, le=20),
                  who: Principal = Depends(principal)) -> dict[str, Any]:
    if not who.scopes & {"assets:read", "assets:publish"}:
        raise IntegrationError(403, "forbidden", "token does not grant this access")
    bus: EventBus = request.app.state.studio.events
    if cursor is None:
        return {"cursor": encode_cursor(bus.epoch, bus.seq), "events": [], "reset_required": False}
    epoch, pos = decode_cursor(cursor)
    if epoch != bus.epoch or pos > bus.seq or bus.since(pos, timeout=0)[1]:
        return _reset(bus)
    deadline = time.monotonic() + timeout_s
    while True:
        pos, events = _collect(bus, pos, who.library_ids)
        if events or time.monotonic() >= deadline or await request.is_disconnected():
            return {"cursor": encode_cursor(bus.epoch, pos), "events": events, "reset_required": False}
        await asyncio.sleep(min(POLL_S, max(0.0, deadline - time.monotonic())))
