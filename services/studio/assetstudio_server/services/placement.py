"""Slot eligibility for one operation call (R5, R7, R10): hard constraints filter, soft score orders."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from assetstudio_core.canonical import sha256_json
from assetstudio_protocol.execution import OPERATION_CAPABILITY, OPERATION_VERSIONS, Requirements

from ..models import load_lock
from ..studio import Studio
from ._runner_util import expired, now_dt, parse

# A slot stays occupied while an attempt that may still touch its GPU is bound to it (spooled+ already released).
_OCCUPYING = ("offered", "leased", "admitted", "executing", "uncertain")
_SLOT_STATES = ("ready", "loaded", "busy")
_catalog_cache: dict[Path, tuple[int, str]] = {}


@dataclass(frozen=True)
class SlotChoice:
    runner_id: str
    session_id: str
    slot_id: str
    device_uuids: tuple[str, ...]
    score: int


def catalog_sha256(studio: Studio) -> str:
    path = studio.settings.config_dir / "models.lock.yaml"
    mtime = path.stat().st_mtime_ns
    cached = _catalog_cache.get(path)
    if cached is None or cached[0] != mtime:
        cached = (mtime, sha256_json(load_lock(studio.settings.config_dir)))
        _catalog_cache[path] = cached
    return cached[1]


def session_fresh(studio: Studio, session: dict[str, Any]) -> bool:
    return parse(session["last_seen_at"]) >= now_dt() - timedelta(seconds=studio.settings.runner_lease_s)


def _model_problem(identity: str, receipts: dict[str, dict[str, Any]], catalog_sha: str) -> str | None:
    """Identity is "key@hash12" as built by coordinator.stages.base.model_identity: hash12 is the first 12 hex
    chars of the receipt's files_sha256 (the runner hashes the same lock entry), so key and prefix must both match."""
    key, _, hash12 = identity.partition("@")
    r = receipts.get(key)
    if r is None or r["status"] != "ok":
        return f"model {key} not verified ({'absent' if r is None else r['status']})"
    if r["catalog_sha256"] != catalog_sha:
        return f"model {key} verified against another catalog"
    if hash12 and r["files_sha256"][:12] != hash12:
        return f"model {key} has a different revision than required"
    return None


def slot_problem(slot: dict[str, Any], devices: dict[str, dict[str, Any]], busy: set[str], operation: str,
                 *, transient: bool = True) -> str | None:
    """Why `slot` cannot serve `operation`. `transient=False` ignores occupancy (a reserved device, a slot that
    already has an attempt): readiness asks whether the slot could ever serve, placement whether it can now."""
    if slot["capability"] != OPERATION_CAPABILITY[operation]:
        return "wrong capability"
    wanted = {"op": operation, "version": OPERATION_VERSIONS[operation]}
    if not any(wanted in e["operations"] for e in slot["engines"]):
        return f"no engine offers {operation} v{wanted['version']}"
    if slot["state"] not in _SLOT_STATES:
        return f"slot state {slot['state']}"
    free = ("free",) if transient else ("free", "reserved")
    if any(devices.get(u, {}).get("claim") not in free for u in slot["device_uuids"]):
        return "device not free (reserved or uncertain)"
    if transient and slot["slot_id"] in busy:
        return "slot already has an attempt"
    return None


def _runner_problem(studio: Studio, runner: dict[str, Any], group: dict[str, Any] | None, operation: str,
                    project_id: str, requirements: Requirements) -> tuple[str | None, dict[str, Any] | None]:
    if group is None:
        return "no runner group", None
    if group["projects"] != "*" and project_id not in group["projects"]:
        return "project not allowed for runner group", None
    if group["operations"] != "*" and operation not in group["operations"]:
        return "operation not allowed for runner group", None
    if not set(requirements.labels) <= set(group["labels"]):
        return "runner group lacks required labels", None
    session = studio.journal.runners.active_session(runner["id"])
    if session is None or not session_fresh(studio, session):
        return "no fresh session", None
    if session["inventory"] is None:
        return "no inventory yet", None
    return None, session


def eligible_slots(studio: Studio, *, operation: str, requirements: Requirements, project_id: str,
                   preferred: tuple[str, str] | None = None) -> tuple[list[SlotChoice], list[str]]:
    """(choices best-first, reasons other slots were excluded). Reasons feed the task's "why not running"."""
    choices: list[SlotChoice] = []
    reasons: list[str] = []
    catalog_sha = catalog_sha256(studio) if requirements.models else ""
    for runner in studio.auth.runners():
        name = runner["name"]
        if runner["state"] != "active":
            continue
        problem, session = _runner_problem(studio, runner, studio.auth.get_group(runner["group_id"]), operation,
                                           project_id, requirements)
        if problem or session is None:
            reasons.append(f"runner {name}: {problem}")
            continue
        receipts = {m["key"]: m for m in session["inventory"].get("models", [])}
        bad = next((p for i in requirements.models if (p := _model_problem(i, receipts, catalog_sha))), None)
        if bad:
            reasons.append(f"runner {name}: {bad}")
            continue
        devices = {d["uuid"]: d for d in studio.journal.runners.devices(runner["id"])}
        now = now_dt()
        busy = {a["slot_id"] for a in studio.journal.attempts.list(states=_OCCUPYING, runner_id=runner["id"])
                if not (a["state"] == "offered" and expired(a["offer_expires_at"], now))}
        for slot in studio.journal.runners.slots(runner["id"]):
            if (p := slot_problem(slot, devices, busy, operation)):
                reasons.append(f"runner {name} slot {slot['slot_id']}: {p}")
                continue
            score = (10 if requirements.resource_profile and slot["loaded_residency"] == requirements.resource_profile
                     else 0) + (5 if (runner["id"], slot["slot_id"]) == preferred else 0)
            choices.append(SlotChoice(runner["id"], session["id"], slot["slot_id"], tuple(slot["device_uuids"]),
                                      score))
    choices.sort(key=lambda c: (-c.score, c.runner_id, c.slot_id))
    return choices, reasons
