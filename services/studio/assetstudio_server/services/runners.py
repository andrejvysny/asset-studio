"""Runner identity, sessions and inventory (R3, R4, R6, R7); slot placement lives in `placement`."""
from __future__ import annotations

import secrets
import threading
from typing import Any

from assetstudio_client import keys as keylib
from assetstudio_core.canonical import sha256_json
from assetstudio_protocol.inventory import Inventory
from assetstudio_protocol.runners import (
    AccessToken,
    Challenge,
    ChallengeRequest,
    InventoryAck,
    RegisterRequest,
    RegisterResponse,
    SessionAccepted,
    SessionHello,
    StudioKey,
    TokenRequest,
    challenge_message,
)
from assetstudio_protocol.versions import SUPPORTED_VERSIONS, negotiate

from ..authstore import RegistrationRefused, Unauthorized
from ..models import load_lock
from ..runner_errors import RunnerError
from ..runnerstore import DeviceConflict
from ..studio import Studio
from .placement import SlotChoice, eligible_slots, session_fresh

__all__ = ["SlotChoice", "eligible_slots", "session_fresh"]

_keys_lock = threading.Lock()
_TERMINAL_FOR_RELEASE = ("lost", "failed", "cancelled", "committed", "quarantined")


def ensure_studio_keys(studio: Studio) -> list[StudioKey]:
    with _keys_lock:
        if not any(k["status"] == "current" for k in studio.auth.studio_keys()):
            private = keylib.generate_private_key()
            studio.auth.add_studio_key(secrets.token_hex(4), private, keylib.public_key_b64(private), "current")
        return [StudioKey(key_id=k["key_id"], public_key=k["public_key"], status=k["status"])
                for k in studio.auth.studio_keys()]


def register(studio: Studio, req: RegisterRequest) -> RegisterResponse:
    try:
        runner = studio.auth.register_runner(req.registration_token, req.public_key, req.name,
                                             req.platform.model_dump())
    except RegistrationRefused as e:
        raise RunnerError(403, "unauthorized", e.reason) from e
    group = studio.auth.get_group(runner["group_id"]) or {}
    return RegisterResponse(runner_id=runner["id"], group_id=runner["group_id"],
                            ephemeral=bool(group.get("ephemeral")), studio_keys=ensure_studio_keys(studio))


def challenge(studio: Studio, req: ChallengeRequest) -> Challenge:
    audience = studio.settings.runner_audience
    try:
        nonce, expires = studio.auth.issue_nonce(req.runner_id, audience)
    except Unauthorized as e:
        raise RunnerError(401, "unauthorized", e.reason) from e
    return Challenge(nonce=nonce, expires_at=expires, audience=audience)


def _refuse_token(studio: Studio, runner_id: str, reason: str) -> RunnerError:
    studio.auth.audit("token_refused", runner_id, runner_id, {"reason": reason})
    return RunnerError(401, "unauthorized", reason)


def issue_token(studio: Studio, req: TokenRequest) -> AccessToken:
    audience = studio.settings.runner_audience
    runner = studio.auth.get_runner(req.runner_id)
    if runner is None or runner["state"] != "active":
        raise _refuse_token(studio, req.runner_id, "unknown_runner" if runner is None else "revoked")
    # Signature first: an unauthenticated caller must not be able to burn a runner's outstanding nonces.
    if not keylib.verify(runner["public_key"], challenge_message(req.runner_id, req.nonce, audience), req.signature):
        raise _refuse_token(studio, req.runner_id, "bad_signature")
    if not studio.auth.consume_nonce(req.nonce, req.runner_id, audience):
        raise _refuse_token(studio, req.runner_id, "nonce_invalid")
    try:
        token, expires = studio.auth.issue_access_token(req.runner_id, studio.settings.runner_token_ttl_s)
    except Unauthorized as e:
        raise _refuse_token(studio, req.runner_id, e.reason) from e
    return AccessToken(access_token=token, expires_at=expires)


def authenticate(studio: Studio, bearer: str | None) -> dict[str, Any]:
    token = (bearer or "").removeprefix("Bearer ").strip()
    runner = studio.auth.resolve_access_token(token) if token else None
    if runner is None:
        raise RunnerError(401, "unauthorized", "invalid_token")
    studio.auth.touch_runner(runner["id"])
    return runner


def require_session(studio: Studio, runner: dict[str, Any], session_id: str) -> dict[str, Any]:
    session = studio.journal.runners.get_session(session_id)
    if session is None or session["runner_id"] != runner["id"] or session["state"] != "active":
        raise RunnerError(409, "stale_session", "session is not the runner's active session")
    return session


def open_session(studio: Studio, runner: dict[str, Any], hello: SessionHello) -> SessionAccepted:
    from . import attempts  # attempts imports this module for require_session

    if hello.runner_id != runner["id"]:
        raise RunnerError(403, "forbidden_scope", "hello is for another runner")
    version = negotiate(hello.protocol_versions)
    if version is None:
        raise RunnerError(409, "protocol_incompatible", "no common protocol version",
                          {"supported": sorted(SUPPORTED_VERSIONS)})
    session, _ = studio.journal.runners.open_session(runner["id"], hello.boot_id, version, hello.dispatch,
                                                     hello.platform.model_dump(), hello.software)
    if session["state"] != "active":
        raise RunnerError(409, "stale_session", "this boot was superseded by a newer session")
    attempts.reconcile_session(studio, runner, session, hello.local_attempts)
    catalog = load_lock(studio.settings.config_dir)
    s = studio.settings
    return SessionAccepted(session_id=session["id"], protocol_version=version, catalog_sha256=sha256_json(catalog),
                           catalog=catalog, studio_keys=ensure_studio_keys(studio), heartbeat_s=s.runner_heartbeat_s,
                           lease_s=s.runner_lease_s, offer_ttl_s=s.runner_offer_ttl_s)


def _release_after_barrier(studio: Studio, runner: dict[str, Any], session: dict[str, Any],
                           advertised: set[str]) -> None:
    """A claim is freed only by barrier evidence (R6/R7): a NEW session (not the one the attempt ran under)
    re-advertising the device. A live session re-sending inventory proves nothing about a stuck engine."""
    for dev in studio.journal.runners.devices(runner["id"]):
        if dev["claim"] != "uncertain" or dev["uuid"] not in advertised or not dev["claim_attempt"]:
            continue
        attempt = studio.journal.attempts.get(dev["claim_attempt"])
        if attempt and attempt["state"] in _TERMINAL_FOR_RELEASE and attempt["session_id"] != session["id"]:
            studio.journal.runners.release_device(dev["uuid"], attempt["id"])


def put_inventory(studio: Studio, runner: dict[str, Any], session_id: str, inv: Inventory) -> InventoryAck:
    session = require_session(studio, runner, session_id)
    if inv.revision <= session["inventory_revision"]:
        return InventoryAck(accepted=False, revision=session["inventory_revision"])
    data = inv.model_dump(mode="json")
    try:
        studio.journal.runners.claim_devices(runner["id"], data["devices"])
    except DeviceConflict as e:
        raise RunnerError(409, "forbidden_scope", str(e), {"uuid": e.uuid, "owner": e.owner}) from e
    studio.journal.runners.replace_slots(runner["id"], data["slots"])
    accepted = studio.journal.runners.set_inventory(session_id, inv.revision, data)
    _release_after_barrier(studio, runner, session, {d.uuid for d in inv.devices})
    studio.auth.audit("inventory", runner["id"], runner["id"], {"revision": inv.revision, "accepted": accepted})
    return InventoryAck(accepted=accepted, revision=inv.revision)
