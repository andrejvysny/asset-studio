"""Studio-side runner services: identity, sessions, placement, attempt lifecycle, transfers (R3-R10, R14).
Service level only (no HTTP). Engines are simulated; nothing here is GPU evidence."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections import namedtuple
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from assetstudio_client import keys as keylib
from assetstudio_core.canonical import sha256_json
from assetstudio_protocol.execution import (
    AcceptRequest,
    AcquireRequest,
    CompleteRequest,
    InputRef,
    Offer,
    RejectRequest,
    Requirements,
)
from assetstudio_protocol.inventory import Inventory
from assetstudio_protocol.runners import (
    ChallengeRequest,
    Heartbeat,
    LocalAttempt,
    RegisterRequest,
    SessionHello,
    TokenRequest,
    challenge_message,
)
from assetstudio_protocol.transfer import MIB, UploadCreate
from assetstudio_server.adapters.fake import FakeAux, FakeEngine, FakeWorker3d
from assetstudio_server.coordinator.stages.base import model_identity
from assetstudio_server.models import load_lock
from assetstudio_server.runner_errors import RunnerError
from assetstudio_server.services import attempts, runners, transfers
from assetstudio_server.services._runner_util import now_dt
from assetstudio_server.studio import Studio, build_studio
from assetstudio_server.taskstore import NewTask

from tests.conftest import ROOT, make_settings

PLATFORM = {"os": "linux", "arch": "x86_64", "hostname": "box"}
MODEL = "qwen3_vl_8b_instruct"
REQ = Requirements(capability="aux3d", engine="aux")
INPUT = InputRef(sha256="a" * 64, size=3, role="source", mime="image/png")


def _full_sha(key: str) -> str:
    entry = load_lock(ROOT / "config")["models"][key]
    blob = json.dumps({"repo": entry.get("repo"), "revision": entry.get("revision"), "files": entry.get("files")},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class Handle:
    """One registered runner with its key, token and current session."""

    def __init__(self, world: World, name: str, prefix: str, group: dict[str, Any], dispatch: str) -> None:
        self.w, self.name, self.prefix, self.dispatch = world, name, prefix, dispatch
        self.priv = keylib.generate_private_key()
        tok = world.studio.auth.create_registration_token(group["id"], 600, "test")
        resp = runners.register(world.studio, RegisterRequest.model_validate({
            "schema": "assetstudio.runner.register.v1", "registration_token": tok,
            "public_key": keylib.public_key_b64(self.priv), "name": name, "platform": PLATFORM}))
        self.id, self.ephemeral = resp.runner_id, resp.ephemeral
        self.token = self.get_token()
        self.runner = runners.authenticate(world.studio, self.token)
        self.sid = ""

    def sign(self, nonce: str, audience: str) -> str:
        return keylib.sign(self.priv, challenge_message(self.id, nonce, audience))

    def get_token(self) -> str:
        ch = runners.challenge(self.w.studio, ChallengeRequest(runner_id=self.id))
        req = TokenRequest(runner_id=self.id, nonce=ch.nonce, signature=self.sign(ch.nonce, ch.audience))
        return runners.issue_token(self.w.studio, req).access_token

    def hello(self, local: list[LocalAttempt] | None = None, versions: list[int] | None = None) -> SessionHello:
        return SessionHello.model_validate({
            "schema": "assetstudio.runner.session.v1", "runner_id": self.id, "boot_id": str(uuid.uuid4()),
            "protocol_versions": versions or [1], "software": {}, "platform": PLATFORM, "dispatch": self.dispatch,
            "local_attempts": [a.model_dump() for a in local or []]})

    def boot(self, local: list[LocalAttempt] | None = None, models: list[str] | None = None,
             inventory: bool = True) -> str:
        self.sid = runners.open_session(self.w.studio, self.runner, self.hello(local)).session_id
        if inventory:
            self.put_inventory(models)
        return self.sid

    def inventory(self, models: list[str] | None, revision: int, sha: str | None = None) -> Inventory:
        p, sha = self.prefix, sha or sha256_json(load_lock(ROOT / "config"))
        dev = lambda i: {"uuid": f"GPU-{p}-{i}", "index": i, "name": "sim", "memory_mb": 24000}  # noqa: E731
        ops = lambda *o: [{"op": x, "version": 1} for x in o]  # noqa: E731
        return Inventory.model_validate({
            "schema": "assetstudio.runner.inventory.v1", "revision": revision, "observed_at": "2026-01-01T00:00:00Z",
            "devices": [dev(0), dev(1)],
            "slots": [
                {"slot_id": "img", "capability": "image", "device_uuids": [f"GPU-{p}-0"], "state": "ready",
                 "engines": [{"engine": "comfyui", "version": "1", "operations": ops("image.t2i", "image.edit")}]},
                {"slot_id": "aux", "capability": "aux3d", "device_uuids": [f"GPU-{p}-1"], "state": "ready",
                 "loaded_residency": "res-a",
                 "engines": [{"engine": "aux", "version": "1", "operations": ops("aux.enhance", "aux.qa")}]}],
            "models": [{"key": k, "revision": "r", "files_sha256": _full_sha(k), "verification": "full",
                        "status": "ok", "catalog_sha256": sha, "verified_at": "2026-01-01T00:00:00Z"}
                       for k in models or []]})

    def put_inventory(self, models: list[str] | None = None, revision: int = 0, inv: Inventory | None = None):
        return runners.put_inventory(self.w.studio, self.runner, self.sid, inv or self.inventory(models, revision))

    def acquire(self, free_slots: list[str] | None = None) -> Offer | None:
        free = [s["slot_id"] for s in self.w.studio.journal.runners.slots(self.id)] if free_slots is None else free_slots
        return attempts.acquire(self.w.studio, self.runner, self.sid,
                                AcquireRequest(request_id=str(uuid.uuid4()), free_slots=free))

    def accept(self, offer: Offer):
        return attempts.accept(self.w.studio, self.runner, offer.attempt_id,
                               AcceptRequest(session_id=self.sid, generation=offer.generation))

    def beat(self, *reports: tuple[str, int, str], lifecycle: str = "active"):
        return attempts.heartbeat(self.w.studio, self.runner, self.sid, Heartbeat.model_validate({
            "session_id": self.sid, "inventory_revision": 0, "lifecycle": lifecycle,
            "attempts": [{"attempt_id": a, "generation": g, "state": s} for a, g, s in reports]}))

    def claims(self) -> set[str]:
        return {d["claim"] for d in self.w.studio.journal.runners.devices(self.id) if d["idx"] == 1}


class World:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.studio: Studio = build_studio(make_settings(tmp), FakeEngine(), FakeAux(), FakeWorker3d())
        self.project_id = self.studio.registry.create("Demo", tmp / "projects" / "demo").id
        self.group = self.studio.auth.create_group("g", "*", "*", ["gpu"], False, "test")
        self.n = 0

    def runner(self, name: str = "r1", *, group: dict[str, Any] | None = None, dispatch: str = "pull",
               models: list[str] | None = None, boot: bool = True) -> Handle:
        h = Handle(self, name, f"{name}{uuid.uuid4().hex[:6]}", group or self.group, dispatch)
        if boot:
            h.boot(models=models)
        return h

    def task(self, running: bool = True) -> str:
        self.n += 1
        t = NewTask(project_id=self.project_id, job_id=f"job_{self.n:0>16}", item_id=f"itm_{self.n:0>16}",
                    stage="generate", family="generate", input_key=str(self.n), inputs={}, lane="gpu1",
                    residency="x")
        self.studio.journal.tasks.create([t], "cmd_x")
        if running:
            assert self.studio.journal.tasks.claim(t.id, "pas_x")
        return t.id

    def offer(self, task_id: str | None = None, key: str = "t/enhance", req: Requirements = REQ, **kw: Any):
        return attempts.offer_call(self.studio, task_id=task_id or self.task(), call_key=key,
                                   project_id=self.project_id, operation="aux.enhance", inputs=[INPUT],
                                   params={}, requirements=req, **kw)

    def leased(self, h: Handle, key: str = "t/enhance") -> tuple[dict[str, Any], Offer]:
        a = self.offer(key=key)
        offer = h.acquire()
        assert offer is not None and offer.attempt_id == a["id"]
        assert h.accept(offer).start_authorized
        return a, offer

    def get(self, attempt_id: str) -> dict[str, Any]:
        return self.studio.journal.attempts.get(attempt_id)  # type: ignore[return-value]


@pytest.fixture
def w(tmp_path: Path):
    world = World(tmp_path)
    yield world
    world.studio.close()


@contextmanager
def _raises(code: str, status: int | None = None) -> Iterator[pytest.ExceptionInfo[RunnerError]]:
    with pytest.raises(RunnerError) as info:
        yield info
    e = info.value
    assert e.code == code and (status is None or e.status == status), e.body()


# --- identity ---------------------------------------------------------------------------------------------------
def test_register_and_token_happy_path(w: World) -> None:
    tok = w.studio.auth.create_registration_token(w.group["id"], 600, "test")
    priv = keylib.generate_private_key()
    req = RegisterRequest.model_validate({
        "schema": "assetstudio.runner.register.v1", "registration_token": tok,
        "public_key": keylib.public_key_b64(priv), "name": "r", "platform": PLATFORM})
    resp = runners.register(w.studio, req)
    assert resp.group_id == w.group["id"] and not resp.ephemeral
    assert [k.status for k in resp.studio_keys] == ["current"]
    assert runners.register(w.studio, req).runner_id == resp.runner_id  # idempotent by key
    assert runners.ensure_studio_keys(w.studio) == resp.studio_keys
    h = w.runner("r2", boot=False)
    assert runners.authenticate(w.studio, h.token)["id"] == h.id
    assert runners.authenticate(w.studio, f"Bearer {h.token}")["id"] == h.id
    for bad in (None, "", "nope"):
        with _raises("unauthorized", 401):
            runners.authenticate(w.studio, bad)
    with _raises("unauthorized", 403):
        runners.register(w.studio, req.model_copy(update={"public_key": keylib.public_key_b64(
            keylib.generate_private_key())}))  # token already used


def test_bad_signature_and_nonce_replay(w: World) -> None:
    h = w.runner(boot=False)
    ch = runners.challenge(w.studio, ChallengeRequest(runner_id=h.id))
    forged = keylib.sign(keylib.generate_private_key(), challenge_message(h.id, ch.nonce, ch.audience))
    with _raises("unauthorized") as e:
        runners.issue_token(w.studio, TokenRequest(runner_id=h.id, nonce=ch.nonce, signature=forged))
    assert e.value.message == "bad_signature"
    assert any(a["event"] == "token_refused" for a in w.studio.auth.audit_log(runner_id=h.id))
    good = TokenRequest(runner_id=h.id, nonce=ch.nonce, signature=h.sign(ch.nonce, ch.audience))
    assert runners.issue_token(w.studio, good).access_token  # the forged try did not burn the nonce
    with _raises("unauthorized") as e:
        runners.issue_token(w.studio, good)
    assert e.value.message == "nonce_invalid"


def test_revoked_runner_refused_everywhere(w: World) -> None:
    h = w.runner(boot=False)
    ch = runners.challenge(w.studio, ChallengeRequest(runner_id=h.id))
    assert w.studio.auth.revoke_runner(h.id, "op")
    with _raises("unauthorized"):
        runners.challenge(w.studio, ChallengeRequest(runner_id=h.id))
    with _raises("unauthorized"):
        runners.issue_token(w.studio, TokenRequest(runner_id=h.id, nonce=ch.nonce,
                                                   signature=h.sign(ch.nonce, ch.audience)))
    with _raises("unauthorized"):
        runners.authenticate(w.studio, h.token)


# --- sessions and inventory ---------------------------------------------------------------------------------------
def test_protocol_incompatible_and_foreign_hello(w: World) -> None:
    h = w.runner(boot=False)
    with _raises("protocol_incompatible", 409) as e:
        runners.open_session(w.studio, h.runner, h.hello(versions=[99]))
    assert e.value.detail == {"supported": [1]}
    other = w.runner("r2", boot=False)
    with _raises("forbidden_scope", 403):
        runners.open_session(w.studio, h.runner, other.hello())
    acc = runners.open_session(w.studio, h.runner, h.hello())
    assert acc.catalog_sha256 == sha256_json(acc.catalog) and acc.lease_s > 2 * acc.heartbeat_s


def test_stale_session_after_new_boot(w: World) -> None:
    h = w.runner()
    old = h.sid
    h.boot()
    assert h.sid != old
    with _raises("stale_session", 409):
        runners.put_inventory(w.studio, h.runner, old, h.inventory(None, 5))
    with _raises("stale_session", 409):
        attempts.heartbeat(w.studio, h.runner, old, Heartbeat(session_id=old, inventory_revision=0))
    assert h.beat().inventory_wanted is False
    assert h.put_inventory(revision=0).accepted is False  # non-monotonic


def test_two_runners_same_index_distinct_uuids_and_uuid_conflict(w: World) -> None:
    a, b = w.runner("a"), w.runner("b")
    choices, _ = runners.eligible_slots(w.studio, operation="aux.enhance", requirements=REQ,
                                        project_id=w.project_id)
    assert {c.runner_id for c in choices} == {a.id, b.id} and {c.slot_id for c in choices} == {"aux"}
    c = w.runner("c", boot=False)
    c.prefix = a.prefix  # a second runner advertising the same physical devices
    c.boot(inventory=False)
    with _raises("forbidden_scope", 409) as e:
        c.put_inventory()
    assert e.value.detail["owner"] == a.id and e.value.detail["uuid"].startswith(f"GPU-{a.prefix}")


def test_model_revision_gates_eligibility(w: World) -> None:
    req = Requirements(capability="aux3d", engine="aux", models=[model_identity(ROOT / "config", MODEL)])
    w.runner("none")
    good = w.runner("good", models=[MODEL])
    stale = w.runner("stale", models=[MODEL])
    bad_inv = stale.inventory([MODEL], 1)
    bad = bad_inv.models[0].model_copy(update={"files_sha256": "f" * 64})
    stale.put_inventory(inv=bad_inv.model_copy(update={"models": [bad]}))
    wrong_cat = w.runner("cat")
    wrong_cat.put_inventory(inv=wrong_cat.inventory([MODEL], 1, sha="e" * 64))
    choices, reasons = runners.eligible_slots(w.studio, operation="aux.enhance", requirements=req,
                                              project_id=w.project_id)
    assert [c.runner_id for c in choices] == [good.id]
    text = "\n".join(reasons)
    assert "runner none: model" in text and "runner stale: model" in text and "different revision" in text
    assert "another catalog" in text


def test_hard_constraints_and_soft_score(w: World) -> None:
    gated = w.studio.auth.create_group("gated", [], ["image.t2i"], [], False, "test")
    w.runner("deny", group=gated)
    a, b = w.runner("a"), w.runner("b")
    labelled = Requirements(capability="aux3d", engine="aux", labels=["nope"])
    assert runners.eligible_slots(w.studio, operation="aux.enhance", requirements=labelled,
                                  project_id=w.project_id)[0] == []
    profile = Requirements(capability="aux3d", engine="aux", resource_profile="res-a")
    top, *_ = runners.eligible_slots(w.studio, operation="aux.enhance", requirements=profile,
                                     project_id=w.project_id, preferred=(b.id, "aux"))[0]
    assert (top.runner_id, top.score) == (b.id, 15)
    assert runners.eligible_slots(w.studio, operation="aux.cutout", requirements=REQ,
                                  project_id=w.project_id)[0] == []  # no engine advertises it
    assert {c.runner_id for c in runners.eligible_slots(w.studio, operation="aux.enhance", requirements=REQ,
                                                        project_id=w.project_id)[0]} == {a.id, b.id}


# --- offers, accept ---------------------------------------------------------------------------------------------
def test_offer_place_acquire_same_request_id(w: World) -> None:
    h = w.runner()
    a = w.offer()
    assert a["generation"] == 1 and a["runner_id"] == h.id and a["slot_id"] == "aux"
    req = AcquireRequest(request_id=str(uuid.uuid4()), free_slots=["aux"])
    first = attempts.acquire(w.studio, h.runner, h.sid, req)
    again = attempts.acquire(w.studio, h.runner, h.sid, req)
    assert first is not None and again == first and first.attempt_id == a["id"]
    assert w.offer(task_id=a["task_id"])["id"] == a["id"]  # idempotent per call_key
    with _raises("stale_revision", 409):
        attempts.offer_call(w.studio, task_id=a["task_id"], call_key="t/enhance", project_id=w.project_id,
                            operation="aux.enhance", inputs=[INPUT], params={"x": 1}, requirements=REQ)
    h.accept(first)
    assert h.acquire() is None


def test_unplaceable_offer_records_reasons_then_places(w: World) -> None:
    a = w.offer()
    assert a["runner_id"] is None and a["progress"]["placement"]["reasons"] == []
    h = w.runner()
    assert attempts.place_pending(w.studio) == 1
    assert w.get(a["id"])["runner_id"] == h.id and "placement" not in w.get(a["id"])["progress"]
    assert attempts.place(w.studio, a["id"]) is False  # already placed and live


def test_accept_replay_and_expiry(w: World) -> None:
    h = w.runner()
    a, offer = w.leased(h)
    again = h.accept(offer)
    assert again.start_authorized and w.get(a["id"])["state"] == "leased"
    assert h.claims() == {"reserved"}
    # cancel intent: replay no longer authorizes the start
    attempts.cancel(w.studio, a["id"])
    assert not h.accept(offer).start_authorized


def test_accept_after_offer_expiry_is_stale_then_replaced(w: World) -> None:
    h = w.runner()
    a = w.offer()
    offer = h.acquire()
    assert offer is not None
    w.studio.journal.attempts.transition(a["id"], ("offered",), "offered", offer_expires_at="2000-01-01T00:00:00Z")
    with _raises("stale_generation", 409):
        h.accept(offer)
    assert h.claims() == {"free"}
    time.sleep(0.01)  # timestamps have ms resolution
    fresh = h.acquire()  # re-placed on the same (only) slot with a new deadline
    assert fresh is not None and fresh.offer_expires_at > offer.offer_expires_at
    assert h.accept(fresh).start_authorized


def test_cancel_before_accept_wins(w: World) -> None:
    h = w.runner()
    a = w.offer()
    offer = h.acquire()
    assert offer and attempts.cancel(w.studio, a["id"])
    assert w.get(a["id"])["state"] == "cancelled" and w.get(a["id"])["disposition"] is None
    with _raises("cancelled_by_operator", 409):
        h.accept(offer)
    assert h.claims() == {"free"}


def test_accept_requires_running_task_and_rolls_back(w: World) -> None:
    h = w.runner()
    w.offer(task_id=w.task(running=False))
    offer = h.acquire()
    assert offer is not None
    with _raises("cancelled_by_operator") as e:
        h.accept(offer)
    assert e.value.message == "task not running" and h.claims() == {"free"}
    assert w.get(offer.attempt_id)["state"] == "offered"


def test_reservation_blocks_second_attempt_on_slot(w: World) -> None:
    h = w.runner()
    w.leased(h)
    second = w.offer(key="t/two")
    assert second["runner_id"] is None
    assert any("device not free" in r for r in second["progress"]["placement"]["reasons"])
    with _raises("forbidden_scope", 403):  # unplaced: not this runner's attempt
        attempts.accept(w.studio, h.runner, second["id"], AcceptRequest(session_id=h.sid, generation=1))


def test_reject_unplaces_with_cooldown(w: World) -> None:
    h = w.runner()
    a = w.offer()
    offer = h.acquire()
    assert offer is not None
    attempts.reject(w.studio, h.runner, a["id"], RejectRequest(session_id=h.sid, generation=1,
                                                               reason="admission_busy"))
    row = w.get(a["id"])
    assert row["runner_id"] is None and row["progress"]["rejections"][0]["reason"] == "admission_busy"
    assert h.acquire() is None  # the slot that just rejected is not re-offered at once
    assert any("rejected" in r for r in w.get(a["id"])["progress"]["placement"]["reasons"])
    other = w.runner("o")
    assert other.acquire() is not None


# --- heartbeat, expiry, loss ---------------------------------------------------------------------------------------
def test_heartbeat_renews_lease_controls_and_receipts(w: World) -> None:
    h = w.runner()
    a, offer = w.leased(h)
    before = w.get(a["id"])["lease_until"]
    time.sleep(0.01)
    resp = h.beat((a["id"], 1, "executing"))
    row = w.get(a["id"])
    assert row["state"] == "executing" and row["lease_until"] > before and resp.controls == []
    h.beat((a["id"], 1, "admitted"))  # never backwards
    assert w.get(a["id"])["state"] == "executing"
    attempts.cancel(w.studio, a["id"])
    assert [c.control for c in h.beat((a["id"], 1, "executing")).controls] == ["cancel"]
    h.beat((a["id"], 1, "cancelled"))
    row = w.get(a["id"])
    assert row["state"] == "cancelled" and h.claims() == {"free"}
    w.studio.journal.attempts.set_disposition(a["id"], "cancelled")
    assert [r.disposition for r in h.beat().receipts] == ["cancelled"] and h.beat().receipts == []
    assert attempts.receipt(w.studio, h.runner, a["id"]).disposition == "cancelled"  # type: ignore[union-attr]
    assert offer.generation == 1


def test_spooled_report_releases_device(w: World) -> None:
    h = w.runner()
    a, _ = w.leased(h)
    h.beat((a["id"], 1, "executing"))
    assert h.claims() == {"reserved"}
    h.beat((a["id"], 1, "spooled"))
    assert h.claims() == {"free"} and w.get(a["id"])["state"] == "spooled"


def test_lease_expiry_keeps_devices_until_barrier_a10(w: World) -> None:
    h = w.runner()
    a, _ = w.leased(h)
    h.beat((a["id"], 1, "executing"))
    assert attempts.expire(w.studio, now=now_dt() + timedelta(hours=1)) == {"uncertain": 1, "unplaced": 0}
    assert w.get(a["id"])["state"] == "uncertain"
    assert h.claims() == {"uncertain"}
    same = w.offer(task_id=a["task_id"])
    assert same["id"] == a["id"]  # never re-placed while uncertain
    assert attempts.declare_lost(w.studio, a["id"], "op") and h.claims() == {"uncertain"}
    gen2 = w.offer(task_id=a["task_id"])
    assert gen2["generation"] == 2 and gen2["runner_id"] is None
    # the same live session re-sending inventory is NOT barrier evidence; a new boot is
    h.put_inventory(revision=1)
    assert h.claims() == {"uncertain"}
    h.boot()
    assert h.claims() == {"free"}
    assert attempts.place_pending(w.studio) == 1 and w.get(gen2["id"])["runner_id"] == h.id
    assert w.get(a["id"])["state"] == "lost"


def test_reconcile_session_shapes_a06_a09(w: World) -> None:
    h = w.runner()
    a, _ = w.leased(h)
    h.beat((a["id"], 1, "executing"))
    b = w.offer(key="t/b")
    assert b["runner_id"] is None  # slot busy
    h.boot([LocalAttempt(attempt_id=a["id"], generation=1, state="executing")])  # restart that kept the attempt
    row = w.get(a["id"])
    assert row["state"] == "executing" and row["session_id"] == h.sid and h.claims() == {"reserved"}
    h.boot([])  # restart that lost track of it: never "lost" automatically
    assert w.get(a["id"])["state"] == "uncertain" and h.claims() == {"uncertain"}
    h.beat()
    h.boot([LocalAttempt(attempt_id=a["id"], generation=1, state="lost")])
    assert w.get(a["id"])["state"] == "lost"
    assert h.claims() == {"free"}  # the runner's own report + fresh session re-advertising is the barrier


# --- completion ----------------------------------------------------------------------------------------------------
def _upload(w: World, h: Handle, attempt_id: str, data: bytes, role: str = "result") -> dict[str, Any]:
    sha = hashlib.sha256(data).hexdigest()
    created = transfers.create_upload(w.studio, h.runner, UploadCreate(
        attempt_id=attempt_id, generation=1, sha256=sha, size=len(data), role=role, mime="image/png"))
    cs = created.chunk_size
    for i in range(-(-len(data) // cs)):
        part = data[i * cs:(i + 1) * cs]
        transfers.put_chunk(w.studio, h.runner, created.upload_id, i, [part], hashlib.sha256(part).hexdigest(),
                            len(part))
    transfers.finalize_upload(w.studio, h.runner, created.upload_id)
    return {"name": "out.png", "sha256": sha, "size": len(data), "mime": "image/png"}


def _manifest(attempt_id: str, files: list[dict[str, Any]], gen: int = 1) -> CompleteRequest:
    return CompleteRequest.model_validate({"session_id": "rse_" + "0" * 16, "manifest": {
        "schema": "assetstudio.result.v1", "attempt_id": attempt_id, "generation": gen, "files": files}})


def _complete(h: Handle, attempt_id: str, files: list[dict[str, Any]]):
    req = _manifest(attempt_id, files).model_copy(update={"session_id": h.sid})
    return attempts.complete(h.w.studio, h.runner, attempt_id, req)


def test_complete_commit_receipt_and_deregister(w: World) -> None:
    h = w.runner()
    a, _ = w.leased(h)
    h.beat((a["id"], 1, "executing"))
    with _raises("missing_artifact", 409):
        _complete(h, a["id"], [{"name": "x.png", "sha256": "b" * 64, "size": 1, "mime": "image/png"}])
    f = _upload(w, h, a["id"], os.urandom(1000))
    assert w.get(a["id"])["state"] == "uploading"
    assert _complete(h, a["id"], [f]).state == "ingested" and _complete(h, a["id"], [f]).state == "ingested"
    assert h.claims() == {"free"} and w.get(a["id"])["manifest"]["files"][0]["sha256"] == f["sha256"]
    with _raises("invalid_input", 409):
        attempts.deregister(w.studio, h.runner)  # ingested is not custody-transferred
    assert attempts.commit(w.studio, a["id"]) and w.get(a["id"])["disposition"] == "committed"
    with _raises("invalid_input", 409):
        attempts.deregister(w.studio, h.runner)  # receipt still pending
    assert [r.disposition for r in h.beat().receipts] == ["committed"]
    attempts.deregister(w.studio, h.runner)
    assert w.studio.auth.get_runner(h.id)["state"] == "revoked"  # type: ignore[index]


def test_dispose_rejected_and_cancelled(w: World) -> None:
    h = w.runner()
    a, _ = w.leased(h)
    h.beat((a["id"], 1, "executing"))
    f = _upload(w, h, a["id"], b"abc")
    _complete(h, a["id"], [f])
    assert attempts.dispose(w.studio, a["id"], "rejected")
    row = w.get(a["id"])
    assert row["state"] == "failed" and row["disposition"] == "rejected"


def test_late_complete_of_superseded_generation_is_quarantined_a11(w: World) -> None:
    h = w.runner()
    a, _ = w.leased(h)
    h.beat((a["id"], 1, "executing"))
    f = _upload(w, h, a["id"], os.urandom(500))
    attempts.expire(w.studio, now=now_dt() + timedelta(hours=1))
    attempts.declare_lost(w.studio, a["id"], "op")
    gen2 = w.offer(task_id=a["task_id"])
    assert gen2["generation"] == 2
    assert _complete(h, a["id"], [f]).state == "quarantined"
    row = w.get(a["id"])
    assert row["disposition"] == "quarantined" and w.get(gen2["id"])["state"] == "offered"
    assert [r.disposition for r in h.beat().receipts] == ["quarantined"]
    assert h.claims() == {"uncertain"}  # a lost attempt's claim still waits for barrier evidence


def test_retry_call_and_cancel_paths(w: World) -> None:
    h = w.runner()
    a, _ = w.leased(h)
    h.beat((a["id"], 1, "failed"))
    assert w.get(a["id"])["state"] == "failed" and h.claims() == {"free"}
    assert w.offer(task_id=a["task_id"])["id"] == a["id"]  # failed is returned, not silently retried
    r = attempts.retry_call(w.studio, a["task_id"], "t/enhance")
    assert r["generation"] == 2 and r["runner_id"] == h.id
    with _raises("invalid_input"):
        attempts.retry_call(w.studio, a["task_id"], "t/enhance")


# --- transfers ---------------------------------------------------------------------------------------------------
def _created(w: World, h: Handle, data: bytes, size: int | None = None, sha: str | None = None):
    w.studio.settings.upload_chunk_size = MIB
    a, _ = w.leased(h)
    h.beat((a["id"], 1, "executing"))
    return a, transfers.create_upload(w.studio, h.runner, UploadCreate(
        attempt_id=a["id"], generation=1, sha256=sha or hashlib.sha256(data).hexdigest(),
        size=size or len(data), role="r", mime="image/png"))


def test_upload_chunks_duplicate_conflict_and_finalize(w: World) -> None:
    h = w.runner()
    data = os.urandom(MIB * 2 + 123)
    a, up = _created(w, h, data)
    assert up.chunk_size == MIB and w.get(a["id"])["state"] == "uploading"
    c0, c1, c2 = data[:MIB], data[MIB:2 * MIB], data[2 * MIB:]
    sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731
    put = lambda i, b, s=None, n=None: transfers.put_chunk(  # noqa: E731
        w.studio, h.runner, up.upload_id, i, iter([b[:1000], b[1000:]]), s or sha(b), n if n is not None else len(b))
    assert put(0, c0).status == "stored" and put(0, c0).status == "duplicate"
    with _raises("invalid_input", 409):
        put(0, os.urandom(MIB))  # same index, different bytes
    with _raises("invalid_input", 400):
        put(1, c1, n=MIB - 1)
    with _raises("invalid_input", 400):
        put(1, c1, s="0" * 64)
    with _raises("invalid_input", 400):
        transfers.put_chunk(w.studio, h.runner, up.upload_id, 9, [b"x"], sha(b"x"), 1)
    assert transfers.upload_status(w.studio, h.runner, up.upload_id).received == [0]
    with _raises("invalid_input", 409):
        transfers.finalize_upload(w.studio, h.runner, up.upload_id)  # chunks missing
    put(2, c2)
    put(1, c1)
    receipt = transfers.finalize_upload(w.studio, h.runner, up.upload_id)
    repo = w.studio.registry.get(w.project_id).store.repo
    assert receipt.sha256 == sha(data) and repo.blob_exists(receipt.sha256)
    assert transfers.finalize_upload(w.studio, h.runner, up.upload_id) == receipt
    st = transfers.upload_status(w.studio, h.runner, up.upload_id)
    assert st.state == "finalized" and w.studio.journal.attempts.reserved_bytes() == 0
    assert not (w.tmp / "instance" / "transfers" / "uploads" / f"{up.upload_id}.part").exists()
    other = w.runner("other")
    with _raises("forbidden_scope", 403):
        transfers.upload_status(w.studio, other.runner, up.upload_id)


def test_wrong_full_hash_is_validation_failed(w: World) -> None:
    h = w.runner()
    real = os.urandom(1000)
    a, up = _created(w, h, real, sha="c" * 64)
    transfers.put_chunk(w.studio, h.runner, up.upload_id, 0, [real], hashlib.sha256(real).hexdigest(), len(real))
    with _raises("validation_failed", 400):
        transfers.finalize_upload(w.studio, h.runner, up.upload_id)
    assert transfers.upload_status(w.studio, h.runner, up.upload_id).state == "expired"
    assert not w.studio.registry.get(w.project_id).store.repo.blob_exists("c" * 64)
    retry = transfers.create_upload(w.studio, h.runner, UploadCreate(
        attempt_id=a["id"], generation=1, sha256="c" * 64, size=len(real), role="r", mime="image/png"))
    assert retry.upload_id != up.upload_id  # an expired session never blocks a fresh one for the same file
    assert transfers.upload_status(w.studio, h.runner, retry.upload_id).state == "open"


def test_quotas_and_disk_floor_are_resource_exhausted(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    h = w.runner()
    a, _ = w.leased(h)
    h.beat((a["id"], 1, "executing"))
    req = lambda data: UploadCreate(attempt_id=a["id"], generation=1, sha256=hashlib.sha256(data).hexdigest(),  # noqa: E731
                                    size=len(data), role="r", mime="image/png")
    s = w.studio.settings
    s.upload_quota_bytes = 100
    with _raises("resource_exhausted", 507):
        transfers.create_upload(w.studio, h.runner, req(b"x" * 101))
    s.upload_quota_bytes, s.upload_runner_quota_bytes = 10**9, 100
    with _raises("resource_exhausted", 507):
        transfers.create_upload(w.studio, h.runner, req(b"x" * 101))
    s.upload_runner_quota_bytes = 10**9
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr("shutil.disk_usage", lambda p: usage(10**9, 10**9 - 1000, 1000))
    s.disk_floor_bytes = 500
    with _raises("resource_exhausted", 507):
        transfers.create_upload(w.studio, h.runner, req(b"x" * 600))
    first = transfers.create_upload(w.studio, h.runner, req(b"x" * 400))
    assert transfers.create_upload(w.studio, h.runner, req(b"x" * 400)) == first  # idempotent
    assert w.get(a["id"])["state"] == "uploading"
    w.studio.journal.attempts.transition(a["id"], ("uploading",), "failed")
    with _raises("invalid_input", 409):
        transfers.create_upload(w.studio, h.runner, req(b"x" * 10))  # attempt no longer accepts uploads
    assert transfers.expire_uploads(w.studio) == 0


def test_stage_and_open_input(w: World) -> None:
    sha = transfers.stage_input(w.studio, b"hello")
    assert sha == hashlib.sha256(b"hello").hexdigest() and transfers.stage_input(w.studio, b"hello") == sha
    with transfers.open_input(w.studio, w.project_id, sha) as f:
        assert f.read() == b"hello"
    data = b"in project"
    psha = hashlib.sha256(data).hexdigest()
    import io
    w.studio.registry.get(w.project_id).store.repo.write_blob(io.BytesIO(data), expected_sha256=psha)
    with transfers.open_input(w.studio, w.project_id, psha) as f:
        assert f.read() == data
    with _raises("missing_artifact", 404):
        transfers.open_input(w.studio, w.project_id, "d" * 64)


# --- ephemeral, push -----------------------------------------------------------------------------------------------
def test_ephemeral_runner_second_accept_refused(w: World) -> None:
    eph = w.studio.auth.create_group("eph", "*", "*", [], True, "test")
    h = w.runner("e", group=eph)
    assert h.ephemeral
    a, _ = w.leased(h)
    h.beat((a["id"], 1, "spooled"))
    w.offer(key="t/two")
    offer = h.acquire()
    assert offer is not None and offer.generation == 1
    with _raises("admission_rejected", 409) as e:
        h.accept(offer)
    assert "ephemeral" in e.value.message and h.claims() == {"free"}


def _push_world(w: World, handler: Any, follow: bool = False) -> tuple[Handle, dict[str, Any]]:
    h = w.runner("p", dispatch="push")
    w.studio.auth.set_push_url(h.id, "http://runner.test")
    w.studio.extras["push_http"] = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=follow)
    return h, w.offer()


def test_push_offer_is_signed_and_posted(w: World) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(202)

    h, a = _push_world(w, handler)  # placement itself pushes
    assert len(seen) == 1 and str(seen[0].url) == "http://runner.test/v1/offers"
    offer = Offer.model_validate(json.loads(seen[0].content))
    assert offer.signature and offer.attempt_id == a["id"] and offer.runner_id == h.id
    assert keylib.verify_offer(offer, runners.ensure_studio_keys(w.studio))
    assert attempts.push_offer(w.studio, w.get(a["id"])) is True


def test_push_offer_does_not_follow_redirects_or_raise(w: World) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(307, headers={"location": "http://evil.test/steal"})

    _, a = _push_world(w, handler, follow=True)
    assert seen == ["http://runner.test/v1/offers"]
    assert attempts.push_offer(w.studio, w.get(a["id"])) is False and len(seen) == 2

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    w.studio.extras["push_http"] = httpx.Client(transport=httpx.MockTransport(boom))
    assert attempts.push_offer(w.studio, w.get(a["id"])) is False  # runner can still pull


# --- custody (H13) and transactional upload accounting (H14) --------------------------------------------------------
def _raw_attempt_state(w: World, attempt_id: str, state: str, manifest: bool = True) -> None:
    with w.studio.journal.attempts.txn() as db:
        db.execute("UPDATE attempts SET state=?, manifest=? WHERE id=?",
                   (state, json.dumps({"files": []}) if manifest else None, attempt_id))


def test_terminal_state_and_disposition_are_one_write(w: World) -> None:
    h = w.runner()
    a, _ = w.leased(h)
    _raw_attempt_state(w, a["id"], "ingested")
    store = w.studio.journal.attempts
    calls: list[str] = []
    store.set_disposition = lambda *a_, **k: calls.append("x") or False  # type: ignore[method-assign]
    assert attempts.commit(w.studio, a["id"])
    row = w.get(a["id"])
    assert not calls and row["state"] == "committed" and row["disposition"] == "committed"
    assert [e["event"] for e in store.events(a["id"])][-2:] == ["committed", "disposition"]


def test_repair_custody_fixes_gaps_idempotently(w: World) -> None:
    h = w.runner()
    a, _ = w.leased(h)
    _raw_attempt_state(w, a["id"], "committed")  # pre-existing crash gap: no disposition
    assert w.get(a["id"])["disposition"] is None
    assert attempts.repair_custody(w.studio) == 1
    assert w.get(a["id"])["disposition"] == "committed" and attempts.repair_custody(w.studio) == 0
    assert [r.disposition for r in h.beat().receipts] == ["committed"]
    # ingested attempts of finished tasks are settled with the task's mapping
    for task_state, state, disp in (("succeeded", "committed", "committed"), ("failed", "failed", "rejected"),
                                    ("cancelled", "cancelled", "cancelled")):
        b = w.offer(key=f"t/{task_state}")
        _raw_attempt_state(w, b["id"], "ingested")
        with w.studio.journal.tasks._lock:  # noqa: SLF001
            w.studio.journal.tasks._db.execute("UPDATE stage_tasks SET state=? WHERE id=?",  # noqa: SLF001
                                               (task_state, b["task_id"]))
        assert attempts.repair_custody(w.studio) == 1
        row = w.get(b["id"])
        assert (row["state"], row["disposition"]) == (state, disp)


def test_lost_attempt_late_output_is_quarantined_never_ingested(w: World) -> None:
    h = w.runner()
    a, _ = w.leased(h)
    h.beat((a["id"], 1, "executing"))
    attempts.expire(w.studio, now=now_dt() + timedelta(hours=1))
    assert attempts.declare_lost(w.studio, a["id"], "op")
    f = _upload(w, h, a["id"], os.urandom(300))
    assert w.get(a["id"])["state"] == "lost"  # an upload does not revive it
    assert _complete(h, a["id"], [f]).state == "quarantined"
    row = w.get(a["id"])
    assert row["state"] == "quarantined" and row["disposition"] == "quarantined"
    assert "ingested" not in [e["event"] for e in w.studio.journal.attempts.events(a["id"])]


def test_concurrent_creates_never_exceed_quota(w: World) -> None:
    import threading

    h = w.runner()
    a, _ = w.leased(h)
    h.beat((a["id"], 1, "executing"))
    w.studio.settings.upload_quota_bytes = 1000
    results: list[str] = []
    barrier = threading.Barrier(8)

    def go(i: int) -> None:
        data = bytes([i]) * 400
        barrier.wait()
        try:
            transfers.create_upload(w.studio, h.runner, UploadCreate(
                attempt_id=a["id"], generation=1, sha256=hashlib.sha256(data).hexdigest(), size=400, role="r",
                mime="image/png"))
            results.append("ok")
        except RunnerError as e:
            results.append(e.code)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count("ok") == 2 and results.count("resource_exhausted") == 6
    assert w.studio.journal.attempts.reserved_bytes() == 800


def test_expired_upload_retry_rechecks_quota_and_mismatch_conflicts(w: World) -> None:
    h = w.runner()
    a, _ = w.leased(h)
    h.beat((a["id"], 1, "executing"))
    s, store = w.studio.settings, w.studio.journal.attempts
    s.upload_quota_bytes = 500
    req = lambda data, **kw: UploadCreate(attempt_id=a["id"], generation=1,  # noqa: E731
                                          sha256=hashlib.sha256(data).hexdigest(), size=kw.get("size", len(data)),
                                          role=kw.get("role", "r"), mime="image/png")
    first = transfers.create_upload(w.studio, h.runner, req(b"a" * 400))
    with _raises("invalid_input", 409):
        transfers.create_upload(w.studio, h.runner, req(b"a" * 400, size=399))
    with _raises("invalid_input", 409):
        transfers.create_upload(w.studio, h.runner, req(b"a" * 400, role="other"))
    assert store.set_upload_state(first.upload_id, ("open",), "expired")
    transfers.create_upload(w.studio, h.runner, req(b"b" * 400))  # others now hold the quota
    with _raises("resource_exhausted", 507):
        transfers.create_upload(w.studio, h.runner, req(b"a" * 400))
    assert store.get_upload(first.upload_id)["state"] == "expired"  # the old row is kept on refusal


def test_expire_uploads_skips_in_process_finalize(w: World) -> None:
    h = w.runner()
    a, up = _created(w, h, b"z" * 100)
    store = w.studio.journal.attempts
    with store.txn() as db:
        db.execute("UPDATE uploads SET expires_at='2000-01-01T00:00:00.000Z', state='finalizing' WHERE id=?",
                   (up.upload_id,))
    transfers._finalizing.add(up.upload_id)  # noqa: SLF001
    try:
        assert transfers.expire_uploads(w.studio) == 0
    finally:
        transfers._finalizing.discard(up.upload_id)  # noqa: SLF001
    assert transfers.expire_uploads(w.studio) == 1  # a crashed finalize has no owner
