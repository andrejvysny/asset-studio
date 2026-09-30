"""Shared helpers for the runner service modules: time, attempt state sets, device claims, event-less updates."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from assetstudio_protocol.execution import TERMINAL_STATES

from ..runner_errors import RunnerError
from ..studio import Studio

ORDER = ("offered", "leased", "admitted", "executing", "spooled", "uploading", "ingested")
LIVE = ("leased", "admitted", "executing", "spooled", "uploading")  # lease-bearing states
NON_TERMINAL = tuple(s for s in (*ORDER, "uncertain") if s not in TERMINAL_STATES)
_TOUCH_COLS = frozenset({"progress", "lease_until"})


def now_dt() -> datetime:
    return datetime.now(UTC)


def fmt(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def after(seconds: int, now: datetime | None = None) -> str:
    return fmt((now or now_dt()) + timedelta(seconds=seconds))


def expired(ts: str | None, now: datetime) -> bool:
    return ts is not None and parse(ts) <= now


def touch(studio: Studio, attempt_id: str, from_states: tuple[str, ...], **cols: Any) -> bool:
    """Conditional column update with no event row: heartbeats must not flood the attempt trail."""
    if (bad := set(cols) - _TOUCH_COLS):
        raise ValueError(f"unknown touch columns: {sorted(bad)}")
    sets = ", ".join(f"{k}=?" for k in cols)
    args = [json.dumps(v) if k == "progress" else v for k, v in cols.items()]
    marks = ",".join("?" * len(from_states))
    with studio.journal.attempts.txn() as db:
        n = db.execute(f"UPDATE attempts SET {sets}, updated_at=? WHERE id=? AND state IN ({marks})",
                       [*args, fmt(now_dt()), attempt_id, *from_states]).rowcount
    return n == 1


def held_devices(studio: Studio, attempt: dict[str, Any]) -> list[dict[str, Any]]:
    if attempt["runner_id"] is None:
        return []
    return [d for d in studio.journal.runners.devices(attempt["runner_id"]) if d["claim_attempt"] == attempt["id"]]


def release_claims(studio: Studio, attempt: dict[str, Any]) -> None:
    for d in held_devices(studio, attempt):
        studio.journal.runners.release_device(d["uuid"], attempt["id"])


def mark_claims_uncertain(studio: Studio, attempt: dict[str, Any]) -> None:
    for d in held_devices(studio, attempt):
        studio.journal.runners.mark_device_uncertain(d["uuid"])


def restore_claims(studio: Studio, attempt: dict[str, Any]) -> None:
    """uncertain -> reserved in one statement: releasing then re-reserving would let another attempt win the gap."""
    with studio.journal.attempts.txn() as db:
        db.execute("UPDATE devices SET claim='reserved', updated_at=? WHERE claim='uncertain' AND claim_attempt=?",
                   (fmt(now_dt()), attempt["id"]))


def load_attempt(studio: Studio, runner: dict[str, Any], attempt_id: str) -> dict[str, Any]:
    row = studio.journal.attempts.get(attempt_id)
    if row is None:
        raise RunnerError(404, "invalid_input", f"unknown attempt {attempt_id}")
    if row["runner_id"] != runner["id"]:
        raise RunnerError(403, "forbidden_scope", "attempt is not placed on this runner")
    return row
