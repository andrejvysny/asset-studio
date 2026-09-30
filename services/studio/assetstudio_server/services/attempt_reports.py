"""Runner-reported attempt progress: heartbeat, report, complete, session reconciliation (R6, R8)."""
from __future__ import annotations

from typing import Any

from assetstudio_protocol.execution import (
    AttemptReport,
    CompleteRequest,
    DispositionReceipt,
    ReportAck,
    ReportRequest,
)
from assetstudio_protocol.runners import Control, Heartbeat, HeartbeatResponse, LocalAttempt

from ..runner_errors import RunnerError
from ..studio import Studio
from ._runner_util import (
    LIVE,
    NON_TERMINAL,
    ORDER,
    after,
    load_attempt,
    mark_claims_uncertain,
    release_claims,
    restore_claims,
    touch,
)
from .runners import require_session

_FAILING = ("offered", "leased", "admitted", "executing", "spooled", "uploading", "uncertain")
_GPU_DONE = ("spooled", "uploading")  # the GPU work has ended once outputs are spooled


def _lease(studio: Studio) -> str:
    return after(studio.settings.runner_lease_s)


def _finish(studio: Studio, attempt: dict[str, Any], report: AttemptReport) -> None:
    error = report.error.model_dump() if report.error else None
    if studio.journal.attempts.transition(attempt["id"], _FAILING, report.state, error=error,
                                          event=f"reported_{report.state}"):
        release_claims(studio, attempt)


def mark_lost(studio: Studio, attempt: dict[str, Any], event: str = "lost_reported") -> bool:
    """Claims turn uncertain, not free: only the R7 barrier (a later session re-advertising) releases them."""
    done = studio.journal.attempts.transition(attempt["id"], _FAILING[:-1] + ("uncertain",), "lost", event=event)
    if done:
        mark_claims_uncertain(studio, attempt)
    return done


def _move(studio: Studio, attempt: dict[str, Any], new: str, progress: dict[str, Any]) -> None:
    store = studio.journal.attempts
    if attempt["state"] == "uncertain":
        ok = store.transition(attempt["id"], ("uncertain",), new, progress=progress, lease_until=_lease(studio),
                              event="recovered", detail={"state": new})
        if ok and new in _GPU_DONE:
            release_claims(studio, attempt)
        elif ok:
            restore_claims(studio, attempt)
        return
    if store.transition(attempt["id"], (attempt["state"],), new, progress=progress, lease_until=_lease(studio)):
        if new in _GPU_DONE:
            release_claims(studio, attempt)


def advance(studio: Studio, attempt: dict[str, Any], report: AttemptReport) -> None:
    """Monotonic forward only. A stale generation, terminal attempt or unsupported claim is ignored silently."""
    cur, new = attempt["state"], report.state
    if report.generation != attempt["generation"] or cur not in NON_TERMINAL:
        return
    if new in ("failed", "cancelled"):
        return _finish(studio, attempt, report)
    if new == "lost":
        mark_lost(studio, attempt)
        return
    if new not in LIVE or cur in ("offered", "ingested"):
        return  # only accept leases an offer; only complete ingests
    progress = {**attempt["progress"], "runner": report.progress}
    if cur == "uncertain" or ORDER.index(new) > ORDER.index(cur):
        return _move(studio, attempt, new, progress)
    if cur in LIVE:
        touch(studio, attempt["id"], (cur,), lease_until=_lease(studio),
              **({"progress": progress} if new == cur and report.progress else {}))


def heartbeat(studio: Studio, runner: dict[str, Any], session_id: str, hb: Heartbeat) -> HeartbeatResponse:
    session = require_session(studio, runner, session_id)
    studio.journal.runners.touch_session(session_id, hb.lifecycle)
    for report in hb.attempts:
        attempt = studio.journal.attempts.get(report.attempt_id)
        if attempt and attempt["runner_id"] == runner["id"] and attempt["session_id"] == session_id:
            advance(studio, attempt, report)
    controls = [Control(attempt_id=a["id"], generation=a["generation"], control="cancel")
                for a in studio.journal.attempts.list(states=NON_TERMINAL, runner_id=runner["id"])
                if a["control"] == "cancel"]
    pending = studio.journal.attempts.pending_receipts(runner["id"])
    receipts = [DispositionReceipt(attempt_id=a["id"], generation=a["generation"], disposition=a["disposition"],
                                   at=a["disposition_at"]) for a in pending]
    studio.journal.attempts.mark_receipts_delivered([a["id"] for a in pending])
    return HeartbeatResponse(controls=controls, receipts=receipts, lease_until=_lease(studio),
                             inventory_wanted=session["inventory_revision"] < 0)


def report(studio: Studio, runner: dict[str, Any], attempt_id: str, req: ReportRequest) -> ReportAck:
    require_session(studio, runner, req.session_id)
    attempt = load_attempt(studio, runner, attempt_id)
    latest = studio.journal.attempts.latest(attempt["task_id"], attempt["call_key"]) or attempt
    if req.report.generation < latest["generation"] or req.report.generation != attempt["generation"]:
        raise RunnerError(409, "stale_generation", "a newer generation of this call exists")
    advance(studio, attempt, req.report)
    return ReportAck(state=(studio.journal.attempts.get(attempt_id) or attempt)["state"])


def _check_manifest(studio: Studio, attempt: dict[str, Any], req: CompleteRequest) -> None:
    m = req.manifest
    if m.attempt_id != attempt["id"] or m.generation != attempt["generation"]:
        raise RunnerError(409, "stale_generation", "manifest does not match the attempt")
    for f in m.files:
        up = studio.journal.attempts.find_upload(attempt["id"], attempt["generation"], f.sha256)
        if up is None or up["state"] != "finalized" or up["size"] != f.size:
            raise RunnerError(409, "missing_artifact", f"{f.name} has no finalized upload", {"sha256": f.sha256})


def complete(studio: Studio, runner: dict[str, Any], attempt_id: str, req: CompleteRequest) -> ReportAck:
    require_session(studio, runner, req.session_id)
    attempt = load_attempt(studio, runner, attempt_id)
    if attempt["state"] in ("ingested", "committed", "quarantined"):
        return ReportAck(state=attempt["state"])  # replay of a lost response
    _check_manifest(studio, attempt, req)
    store = studio.journal.attempts
    superseded = (store.latest(attempt["task_id"], attempt["call_key"]) or attempt)["generation"] > attempt[
        "generation"]
    if superseded or attempt["state"] == "lost":
        if store.transition(attempt_id, ("executing", "spooled", "uploading", "uncertain", "lost"), "quarantined",
                            manifest=req.manifest.model_dump(mode="json"), event="quarantined"):
            store.set_disposition(attempt_id, "quarantined")
            if attempt["state"] != "lost":
                release_claims(studio, attempt)
    elif store.transition(attempt_id, ("executing", "spooled", "uploading", "uncertain"), "ingested",
                          manifest=req.manifest.model_dump(mode="json")):
        release_claims(studio, attempt)
    else:
        raise RunnerError(409, "stale_generation", f"attempt is {attempt['state']}")
    return ReportAck(state=(store.get(attempt_id) or attempt)["state"])


def reconcile_session(studio: Studio, runner: dict[str, Any], session: dict[str, Any],
                      local_attempts: list[LocalAttempt]) -> None:
    """Runner boot: Studio's view is reconciled to the runner's. Unlisted live attempts become uncertain, never
    lost automatically: only an operator (or a runner report) may say the work is gone."""
    listed: set[str] = set()
    for la in local_attempts:
        attempt = studio.journal.attempts.get(la.attempt_id)
        if (attempt is None or attempt["runner_id"] != runner["id"] or attempt["generation"] != la.generation
                or attempt["state"] not in NON_TERMINAL):
            continue
        listed.add(attempt["id"])
        if la.state == "lost":
            mark_lost(studio, attempt, "lost_reported")  # keeps the old session binding: barrier evidence, R7
            continue
        with studio.journal.attempts.txn() as db:
            db.execute("UPDATE attempts SET session_id=? WHERE id=?", (session["id"], attempt["id"]))
        advance(studio, {**attempt, "session_id": session["id"]},
                AttemptReport(attempt_id=la.attempt_id, generation=la.generation, state=la.state))
    for attempt in studio.journal.attempts.list(states=LIVE, runner_id=runner["id"]):
        if attempt["id"] not in listed and studio.journal.attempts.transition(
                attempt["id"], (attempt["state"],), "uncertain", event="unlisted_after_reconnect"):
            mark_claims_uncertain(studio, attempt)
