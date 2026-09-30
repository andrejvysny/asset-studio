"""Attempt lifecycle on the Studio side: offer, placement, pull/push delivery, accept (the commit point), cancel,
lease expiry (R5, R6, R8). Runner-reported progress lives in `attempt_reports` and is re-exported here."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Literal

import httpx
from assetstudio_client import keys as keylib
from assetstudio_protocol.execution import (
    OPERATION_VERSIONS,
    AcceptRequest,
    AcceptResponse,
    AcquireRequest,
    DispositionReceipt,
    InputRef,
    Offer,
    Policy,
    RejectRequest,
    Requirements,
    compute_input_digest,
)

from ..attemptstore import AttemptStore, StaleRevision
from ..runner_errors import RunnerError
from ..studio import Studio
from ._runner_util import (
    LIVE,
    NON_TERMINAL,
    after,
    expired,
    fmt,
    load_attempt,
    mark_claims_uncertain,
    now_dt,
    parse,
    touch,
)
from .attempt_reports import complete, heartbeat, reconcile_session, report
from .placement import eligible_slots
from .runners import require_session

__all__ = ["complete", "heartbeat", "reconcile_session", "report"]

_place_lock = threading.Lock()
_acquired: dict[tuple[str, str], tuple[str, float]] = {}
_acquired_lock = threading.Lock()
_push_client = httpx.Client(follow_redirects=False, timeout=5.0)
_SPEC_KEYS = ("task_id", "call_key", "operation", "input_digest", "operation_version", "inputs", "params",
              "requirements", "policy")
_REPLAY = ("leased", "admitted", "executing", "spooled", "uploading")
# Resource problems, and an execution whose outcome is unknowable: the next entry opens a new generation.
_RETRYABLE_CODES = ("node_unavailable", "admission_rejected", "uncertain_execution")


# --- offers -------------------------------------------------------------------------------------------------------
def _spec(operation: str, digest: str, task_id: str, call_key: str, inputs: list[InputRef], params: dict[str, Any],
          requirements: Requirements, policy: Policy) -> dict[str, Any]:
    return {"task_id": task_id, "call_key": call_key, "operation": operation, "input_digest": digest,
            "operation_version": OPERATION_VERSIONS[operation], "inputs": [i.model_dump() for i in inputs],
            "params": params, "requirements": requirements.model_dump(), "policy": policy.model_dump()}


def _create(studio: Studio, *, project_id: str, generation: int, spec: dict[str, Any],
            preferred: tuple[str, str] | None) -> dict[str, Any]:
    try:
        row, created = studio.journal.attempts.create_offer(
            task_id=spec["task_id"], call_key=spec["call_key"], generation=generation, project_id=project_id,
            operation=spec["operation"], operation_version=spec["operation_version"],
            input_digest=spec["input_digest"], offer=spec, runner_id=None, session_id=None, slot_id=None,
            offer_expires_at=None)
    except StaleRevision as e:
        raise RunnerError(409, "stale_revision", str(e)) from e
    if created and preferred:
        touch(studio, row["id"], ("offered",), progress={"preferred": list(preferred)})
    place(studio, row["id"])
    return studio.journal.attempts.get(row["id"]) or row


def _superseded(attempt: dict[str, Any]) -> bool:
    if attempt["state"] in ("lost", "cancelled"):  # a cancelled call re-entered by a retried task starts afresh
        return True
    if attempt["state"] != "failed":
        return False
    return (attempt["error"] or {}).get("code") in _RETRYABLE_CODES or attempt["disposition"] == "rejected"


def offer_call(studio: Studio, *, task_id: str, call_key: str, project_id: str, operation: str,
               inputs: list[InputRef], params: dict[str, Any], requirements: Requirements,
               preferred: tuple[str, str] | None = None) -> dict[str, Any]:
    """Idempotent per (task, call_key): a live, uncertain or finished attempt is returned, never re-placed.
    Only a `lost` or `cancelled` attempt, or one that failed on a retryable resource problem or was rejected by
    Studio's own validation of its output, makes the next call open generation+1."""
    policy = Policy()
    digest = compute_input_digest(operation, OPERATION_VERSIONS[operation], inputs, params, requirements, policy)
    latest = studio.journal.attempts.latest(task_id, call_key)
    if latest is not None and latest["input_digest"] != digest:
        raise RunnerError(409, "stale_revision", f"{task_id}/{call_key} was offered with different inputs")
    if latest is not None and not _superseded(latest):
        return latest
    spec = _spec(operation, digest, task_id, call_key, inputs, params, requirements, policy)
    return _create(studio, project_id=project_id, generation=1 if latest is None else latest["generation"] + 1,
                   spec=spec, preferred=preferred)


def retry_call(studio: Studio, task_id: str, call_key: str) -> dict[str, Any]:
    latest = studio.journal.attempts.latest(task_id, call_key)
    if latest is None or latest["state"] not in ("failed", "cancelled", "lost"):
        raise RunnerError(409, "invalid_input", "only a failed, cancelled or lost call can be retried")
    return _create(studio, project_id=latest["project_id"], generation=latest["generation"] + 1,
                   spec={k: latest["offer"][k] for k in _SPEC_KEYS}, preferred=None)


def _rejected_recently(studio: Studio, attempt: dict[str, Any], now: datetime) -> set[tuple[str, str]]:
    ttl = timedelta(seconds=studio.settings.runner_offer_ttl_s)
    return {(r["runner_id"], r["slot_id"]) for r in attempt["progress"].get("rejections", [])
            if parse(r["at"]) + ttl > now}


def _note_blocked(studio: Studio, attempt: dict[str, Any], reasons: list[str]) -> None:
    if attempt["progress"].get("placement", {}).get("reasons") == reasons:
        return
    progress = {**attempt["progress"], "placement": {"reasons": reasons, "at": fmt(now_dt())}}
    touch(studio, attempt["id"], ("offered",), progress=progress)


def place(studio: Studio, attempt_id: str, now: datetime | None = None) -> bool:
    """True only when a new placement was made. Serialised: two callers must not offer one attempt twice."""
    now = now or now_dt()
    with _place_lock:
        a = studio.journal.attempts.get(attempt_id)
        if a is None or a["state"] != "offered" or a["control"] != "run":
            return False
        if a["runner_id"] is not None and not expired(a["offer_expires_at"], now):
            return False
        spec, prefer = a["offer"], a["progress"].get("preferred")
        choices, reasons = eligible_slots(studio, operation=a["operation"], project_id=a["project_id"],
                                          requirements=Requirements.model_validate(spec["requirements"]),
                                          preferred=tuple(prefer) if prefer else None)
        cooling = _rejected_recently(studio, a, now)
        reasons += [f"slot {s} on {r} rejected this offer recently" for r, s in sorted(cooling)]
        choices = [c for c in choices if (c.runner_id, c.slot_id) not in cooling]
        if not choices:
            _note_blocked(studio, a, reasons)
            return False
        best = choices[0]
        # spec first: a re-placement reads back a previous full offer, whose placement fields must be overridden
        offer = Offer.model_validate({
            **spec, "schema": "assetstudio.execution.v1", "attempt_id": a["id"], "generation": a["generation"],
            "runner_id": best.runner_id, "session_id": best.session_id, "slot_id": best.slot_id,
            "offer_expires_at": after(studio.settings.runner_offer_ttl_s, now), "signature": None})
        progress = {k: v for k, v in a["progress"].items() if k != "placement"}
        placed = studio.journal.attempts.transition(
            a["id"], ("offered",), "offered", runner_id=best.runner_id, session_id=best.session_id,
            slot_id=best.slot_id, offer_expires_at=offer.offer_expires_at, offer=offer.model_dump(mode="json"),
            progress=progress, event="placed", detail={"runner_id": best.runner_id, "slot_id": best.slot_id})
    if placed:
        _maybe_push(studio, attempt_id, best.session_id, best.runner_id)
    return placed


def _maybe_push(studio: Studio, attempt_id: str, session_id: str, runner_id: str) -> None:
    session = studio.journal.runners.get_session(session_id)
    runner = studio.auth.get_runner(runner_id)
    attempt = studio.journal.attempts.get(attempt_id)
    if session and runner and attempt and session["dispatch"] == "push" and runner.get("push_url"):
        push_offer(studio, attempt)


def place_pending(studio: Studio, now: datetime | None = None) -> int:
    now = now or now_dt()
    return sum(place(studio, a["id"], now) for a in studio.journal.attempts.list(states=("offered",))
               if a["runner_id"] is None or expired(a["offer_expires_at"], now))


def push_offer(studio: Studio, attempt: dict[str, Any]) -> bool:
    """Best effort: a failed push is harmless, the runner can still pull. Redirects are never followed (SSRF)."""
    runner = studio.auth.get_runner(attempt["runner_id"]) if attempt["runner_id"] else None
    keys = [k for k in studio.auth.studio_keys(include_private=True) if k["status"] == "current"]
    if runner is None or not runner.get("push_url") or not keys or not attempt["offer"]:
        return False
    try:
        signed = keylib.sign_offer(bytes(keys[0]["private_key"]), Offer.model_validate(attempt["offer"]))
        client = studio.extras.get("push_http") or _push_client
        resp = client.post(f"{runner['push_url'].rstrip('/')}/v1/offers", json=signed.model_dump(mode="json"),
                           follow_redirects=False)
    except (httpx.HTTPError, ValueError):
        return False
    return resp.is_success


# --- pull delivery ------------------------------------------------------------------------------------------------
def _remembered(runner_id: str, request_id: str) -> str | None:
    with _acquired_lock:
        for k in [k for k, (_, exp) in _acquired.items() if exp < time.monotonic()]:
            del _acquired[k]
        hit = _acquired.get((runner_id, request_id))
    return hit[0] if hit else None


def _live_offer(studio: Studio, attempt_id: str | None, runner_id: str, session_id: str,
                now: datetime) -> dict[str, Any] | None:
    a = studio.journal.attempts.get(attempt_id) if attempt_id else None
    ok = (a and a["state"] == "offered" and a["runner_id"] == runner_id and a["session_id"] == session_id
          and a["control"] == "run" and not expired(a["offer_expires_at"], now))
    return a if ok else None


def acquire(studio: Studio, runner: dict[str, Any], session_id: str, req: AcquireRequest,
            now: datetime | None = None) -> Offer | None:
    """One poll; the route implements the long-poll by repeating it. Same request_id -> same offer."""
    now = now or now_dt()
    require_session(studio, runner, session_id)
    place_pending(studio, now)
    a = _live_offer(studio, _remembered(runner["id"], req.request_id), runner["id"], session_id, now)
    if a is None:
        a = next((x for x in studio.journal.attempts.list(states=("offered",), runner_id=runner["id"])
                  if _live_offer(studio, x["id"], runner["id"], session_id, now)), None)
    if a is None:
        return None
    with _acquired_lock:
        _acquired[(runner["id"], req.request_id)] = (
            a["id"], time.monotonic() + studio.settings.runner_offer_ttl_s)
    return Offer.model_validate(a["offer"])


# --- accept / reject ----------------------------------------------------------------------------------------------
def _bound_attempt(studio: Studio, runner: dict[str, Any], row: dict[str, Any], session_id: str,
                   generation: int) -> None:
    active = studio.journal.runners.active_session(runner["id"])
    if active is None or active["id"] != session_id or row["session_id"] != session_id:
        raise RunnerError(409, "stale_session", "session is not the runner's active session for this attempt")
    if row["generation"] != generation:
        raise RunnerError(409, "stale_generation", "generation does not match the attempt")


def _authorize_offer(studio: Studio, db: Any, runner: dict[str, Any], row: dict[str, Any], now: datetime) -> None:
    if expired(row["offer_expires_at"], now):
        raise RunnerError(409, "stale_generation", "offer expired")
    if row["control"] != "run":
        raise RunnerError(409, "cancelled_by_operator", "attempt cancelled")
    task = db.execute("SELECT state, control FROM stage_tasks WHERE id=?", (row["task_id"],)).fetchone()
    if task is None or task["state"] != "running" or task["control"] != "run":
        raise RunnerError(409, "cancelled_by_operator", "task not running")
    group = studio.auth.get_group(runner["group_id"])
    used = db.execute("SELECT 1 FROM attempts a JOIN attempt_events e ON e.attempt_id=a.id WHERE a.runner_id=? "
                      "AND a.id != ? AND e.event='leased' LIMIT 1", (runner["id"], row["id"])).fetchone()
    if group and group["ephemeral"] and used:
        raise RunnerError(409, "admission_rejected", "ephemeral runner already used")


def _reserve_devices(db: Any, row: dict[str, Any]) -> None:
    slot = db.execute("SELECT device_uuids FROM slots WHERE runner_id=? AND slot_id=?",
                      (row["runner_id"], row["slot_id"])).fetchone()
    if slot is None:
        raise RunnerError(409, "admission_rejected", "slot no longer advertised")
    for uuid in json.loads(slot["device_uuids"]):
        n = db.execute("UPDATE devices SET claim='reserved', claim_attempt=?, updated_at=? WHERE uuid=? "
                       "AND claim='free'", (row["id"], fmt(now_dt()), uuid)).rowcount
        if n != 1:
            raise RunnerError(409, "admission_rejected", f"device {uuid} is not free")


def accept(studio: Studio, runner: dict[str, Any], attempt_id: str, req: AcceptRequest,
           now: datetime | None = None) -> AcceptResponse:
    """The only commit point: one journal transaction validates continuation, generation and devices, then leases."""
    now = now or now_dt()
    store = studio.journal.attempts
    with store.txn() as db:
        found = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if found is None or found["runner_id"] != runner["id"]:
            raise RunnerError(404 if found is None else 403, "invalid_input" if found is None else "forbidden_scope",
                              "attempt is not placed on this runner")
        row = dict(found)
        _bound_attempt(studio, runner, row, req.session_id, req.generation)
        if row["state"] == "cancelled":
            raise RunnerError(409, "cancelled_by_operator", "attempt cancelled")
        if row["state"] in _REPLAY:
            live = studio.auth.get_runner(runner["id"])
            ok = bool(row["lease_until"]) and not expired(row["lease_until"], now) and row["control"] == "run" \
                and live is not None and live["state"] == "active"
            return AcceptResponse(attempt_id=attempt_id, generation=row["generation"], start_authorized=ok,
                                  lease_until=row["lease_until"] or "")
        if row["state"] != "offered":
            raise RunnerError(409, "stale_generation", f"attempt is {row['state']}")
        _authorize_offer(studio, db, runner, row, now)
        _reserve_devices(db, row)
        lease = after(studio.settings.runner_lease_s, now)
        db.execute("UPDATE attempts SET state='leased', lease_until=?, updated_at=?, revision=revision+1 "
                   "WHERE id=? AND state='offered'", (lease, fmt(now), attempt_id))
        AttemptStore._event(db, attempt_id, "leased", {"runner_id": runner["id"]})
    return AcceptResponse(attempt_id=attempt_id, generation=row["generation"], start_authorized=True,
                          lease_until=lease)


def reject(studio: Studio, runner: dict[str, Any], attempt_id: str, req: RejectRequest) -> None:
    row = load_attempt(studio, runner, attempt_id)
    _bound_attempt(studio, runner, row, req.session_id, req.generation)
    if row["state"] != "offered":
        raise RunnerError(409, "stale_generation", f"attempt is {row['state']}")
    rejections = [*row["progress"].get("rejections", []),
                  {"runner_id": runner["id"], "slot_id": row["slot_id"], "reason": req.reason,
                   "detail": req.detail, "at": fmt(now_dt())}][-10:]
    studio.journal.attempts.transition(
        attempt_id, ("offered",), "offered", runner_id=None, session_id=None, slot_id=None, offer_expires_at=None,
        progress={**row["progress"], "rejections": rejections}, event="rejected", detail={"reason": req.reason})


# --- control, expiry, loss ----------------------------------------------------------------------------------------
def cancel(studio: Studio, attempt_id: str) -> bool:
    """Nothing started for an offer: it is cancelled at once. Started attempts get a control intent that the
    runner receives on its next heartbeat and acknowledges by reporting `cancelled`."""
    store = studio.journal.attempts
    done = store.set_control(attempt_id, "cancel")
    if done:
        store.transition(attempt_id, ("offered",), "cancelled", event="cancelled_before_start")
    return done


def expire(studio: Studio, now: datetime | None = None) -> dict[str, int]:
    """Leases lapse into `uncertain`; devices are never freed here (I06)."""
    now, counts = now or now_dt(), {"uncertain": 0, "unplaced": 0}
    store = studio.journal.attempts
    for a in store.list(states=LIVE):
        if expired(a["lease_until"], now) and store.transition(a["id"], (a["state"],), "uncertain",
                                                               event="lease_expired"):
            mark_claims_uncertain(studio, a)
            counts["uncertain"] += 1
    for a in store.list(states=("offered",)):
        if a["runner_id"] is not None and expired(a["offer_expires_at"], now) and store.transition(
                a["id"], ("offered",), "offered", runner_id=None, session_id=None, slot_id=None,
                offer_expires_at=None, event="offer_expired"):
            counts["unplaced"] += 1
    return counts


def declare_lost(studio: Studio, attempt_id: str, actor: str) -> bool:
    attempt = studio.journal.attempts.get(attempt_id)
    done = studio.journal.attempts.transition(attempt_id, ("uncertain",), "lost", event="declared_lost",
                                              detail={"actor": actor})
    if done and attempt:
        studio.auth.audit("declare_lost", actor, attempt["runner_id"], {"attempt_id": attempt_id})
    return done


# --- completion by Studio stage code --------------------------------------------------------------------------------
def commit(studio: Studio, attempt_id: str) -> bool:
    done = studio.journal.attempts.transition(attempt_id, ("ingested",), "committed")
    if done:
        studio.journal.attempts.set_disposition(attempt_id, "committed")
    return done


def dispose(studio: Studio, attempt_id: str, disposition: Literal["rejected", "cancelled"]) -> bool:
    state = "failed" if disposition == "rejected" else "cancelled"
    error = {"code": "validation_failed", "message": "output rejected"} if disposition == "rejected" else None
    done = studio.journal.attempts.transition(attempt_id, ("ingested",), state, error=error, event=disposition)
    if done:
        studio.journal.attempts.set_disposition(attempt_id, disposition)
    return done


def receipt(studio: Studio, runner: dict[str, Any], attempt_id: str) -> DispositionReceipt | None:
    a = load_attempt(studio, runner, attempt_id)
    if a["disposition"] is None:
        return None
    studio.journal.attempts.mark_receipts_delivered([a["id"]])
    return DispositionReceipt(attempt_id=a["id"], generation=a["generation"], disposition=a["disposition"],
                              at=a["disposition_at"])


def deregister(studio: Studio, runner: dict[str, Any]) -> None:
    store = studio.journal.attempts
    if store.list(states=NON_TERMINAL, runner_id=runner["id"]) or store.pending_receipts(runner["id"]):
        raise RunnerError(409, "invalid_input", "custody not transferred")
    studio.auth.revoke_runner(runner["id"], actor=runner["id"])

