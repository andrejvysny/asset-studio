"""Runner HTTP surface (R17) end to end: the real runner agent and client drive the real Studio app in-process.
Engines are SIMULATED (FakeExecutor): contract evidence only, never GPU proof."""
from __future__ import annotations

import hashlib
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from assetstudio_client import ApiError, RunnerClient, keys
from assetstudio_node.agent import RunnerAgent, load_or_create_key
from assetstudio_node.config import RunnerConfig
from assetstudio_node.executor import FakeExecutor
from assetstudio_node.spool import Spool
from assetstudio_node.state import RunnerState
from assetstudio_protocol.execution import (
    AcceptRequest,
    AcquireRequest,
    AttemptReport,
    InputRef,
    Offer,
    ReportRequest,
    Requirements,
)
from assetstudio_protocol.inventory import Inventory
from assetstudio_protocol.runners import RegisterRequest, SessionHello
from assetstudio_protocol.transfer import MAX_CHUNK, UploadCreate
from assetstudio_server.services import attempts, transfers
from assetstudio_server.services._runner_util import now_dt
from assetstudio_server.services.runner_maintenance import RunnerMaintenance
from assetstudio_server.taskstore import NewTask

from tests.conftest import Api, new_project

BASE = "http://testserver"
P = "/api/runner/v1"
PLATFORM = {"os": "linux", "arch": "x86_64", "hostname": "box"}
REQ = Requirements(capability="aux3d", engine="aux")


@dataclass
class Env:
    api: Api
    pid: str
    n: int = 0

    def task(self) -> str:
        self.n += 1
        t = NewTask(project_id=self.pid, job_id=f"job_{self.n:0>16}", item_id=f"itm_{self.n:0>16}", stage="generate",
                    family="generate", input_key=str(self.n), inputs={}, lane="gpu1", residency="x")
        self.api.studio.journal.tasks.create([t], "cmd_x")
        assert self.api.studio.journal.tasks.claim(t.id, "pas_x")
        return t.id

    def offer(self, data: bytes = b"0123456789abcdef", key: str = "k1", task: str | None = None) -> dict[str, Any]:
        sha = transfers.stage_input(self.api.studio, data)
        return attempts.offer_call(
            self.api.studio, task_id=task or self.task(), call_key=key, project_id=self.pid, operation="aux.cutout",
            inputs=[InputRef(sha256=sha, size=len(data), role="source", mime="image/png")], params={},
            requirements=REQ)


@pytest.fixture
def env(make_api: Callable[..., Api]) -> Env:
    api = make_api(coordinator=False)
    return Env(api, new_project(api))


def make_group(api: Api, name: str = "g") -> str:
    return api.post("/api/v1/runner-groups", {"name": name, "projects": "*", "operations": "*", "labels": [],
                                              "ephemeral": False})["id"]


def reg_token(api: Api, group_id: str) -> str:
    return api.post(f"/api/v1/runner-groups/{group_id}/registration-tokens", {"ttl_s": 600})["token"]


class Node:
    """A raw protocol client (no agent loop) for one registered runner."""

    def __init__(self, env: Env, name: str = "r1", device: str | None = None, group_id: str | None = None) -> None:
        self.env, self.name, self.device = env, name, device or f"GPU-{uuid.uuid4().hex[:8]}"
        self.priv = keys.generate_private_key()
        self.client = RunnerClient(BASE, private_key=self.priv, http=env.api.c)
        gid = group_id or make_group(env.api, f"g-{name}")
        out = self.client.register(RegisterRequest.model_validate({
            "schema": "assetstudio.runner.register.v1", "registration_token": reg_token(env.api, gid),
            "public_key": keys.public_key_b64(self.priv), "name": name, "platform": PLATFORM}))
        self.id, self.sid, self.generation = out.runner_id, "", 1
        self.client.token()

    def auth(self) -> dict[str, str]:
        return self.client._auth_headers()

    def hello(self, versions: list[int] | None = None) -> SessionHello:
        return SessionHello.model_validate({
            "schema": "assetstudio.runner.session.v1", "runner_id": self.id, "boot_id": str(uuid.uuid4()),
            "protocol_versions": versions or [1], "software": {}, "platform": PLATFORM, "dispatch": "pull"})

    def inventory(self, revision: int = 1) -> Inventory:
        return Inventory.model_validate({
            "schema": "assetstudio.runner.inventory.v1", "revision": revision, "observed_at": "2026-01-01T00:00:00Z",
            "devices": [{"uuid": self.device, "index": 0, "name": "sim", "memory_mb": 24000}],
            "slots": [{"slot_id": "aux", "capability": "aux3d", "device_uuids": [self.device], "state": "ready",
                       "engines": [{"engine": "aux", "version": "1",
                                    "operations": [{"op": "aux.cutout", "version": 1}]}]}]})

    def boot(self, inventory: bool = True) -> str:
        self.sid = self.client.open_session(self.hello()).session_id
        if inventory:
            self.client.put_inventory(self.sid, self.inventory())
        return self.sid

    def acquire(self, wait_s: int = 0) -> Offer | None:
        return self.client.acquire(self.sid, AcquireRequest(request_id=str(uuid.uuid4()), wait_s=wait_s))

    def accept(self, offer: Offer) -> Any:
        return self.client.accept(offer.attempt_id, AcceptRequest(session_id=self.sid, generation=offer.generation))

    def lease(self, **kw: Any) -> Offer:
        self.env.offer(**kw)
        offer = self.acquire()
        assert offer is not None and self.accept(offer).start_authorized
        return offer

    def report(self, offer: Offer, state: str) -> None:
        self.client.report(offer.attempt_id, ReportRequest(session_id=self.sid, report=AttemptReport(
            attempt_id=offer.attempt_id, generation=offer.generation, state=state)))  # type: ignore[arg-type]

    def raw(self, method: str, path: str, **kw: Any) -> Any:
        return self.env.api.c.request(method, f"{P}{path}", headers={**self.auth(), **kw.pop("headers", {})}, **kw)


def claims(env: Env, device: str) -> str:
    return next(d["claim"] for d in env.api.studio.journal.runners.devices() if d["uuid"] == device)


# --- operator API + auth boundaries -----------------------------------------------------------------------------
def test_operator_and_runner_auth_boundaries(env: Env) -> None:
    api = env.api
    body = {"name": "g", "projects": "*", "operations": ["aux.cutout"], "labels": ["gpu"], "ephemeral": False}
    assert api.c.post("/api/v1/runner-groups", json=body).status_code == 403  # CSRF gate unchanged
    group = api.post("/api/v1/runner-groups", body)
    assert group["operations"] == ["aux.cutout"] and group["created_by"] == "operator"
    assert api.post("/api/v1/runner-groups", body, status=409)["error"]["code"] == "conflict"
    assert [g["id"] for g in api.get("/api/v1/runner-groups")["groups"]] == [group["id"]]
    assert api.c.post(f"/api/v1/runner-groups/{group['id']}/registration-tokens", json={}).status_code == 403
    tok = api.post(f"/api/v1/runner-groups/{group['id']}/registration-tokens", {})
    assert tok["group_id"] == group["id"] and len(tok["token"]) >= 16
    api.post(f"/api/v1/runner-groups/{group['id']}/registration-tokens", {"ttl_s": 5}, status=400)
    api.post("/api/v1/runner-groups/rgp_nope/registration-tokens", {}, status=404)
    # runner routes: no CSRF header needed, but a bearer is
    assert api.c.post(f"{P}/sessions", json={}).status_code == 401
    r = api.c.post(f"{P}/sessions", json={}, headers={"Authorization": "Basic abc"})
    assert r.status_code == 401 and r.json()["code"] == "unauthorized"
    r = api.c.post(f"{P}/sessions", json={}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    r = api.c.post(f"{P}/token/challenge", json={"runner_id": "bogus"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_input"  # flat ErrorBody, not {"error": ...}
    assert api.get("/api/health")["ok"] is True


def test_session_hello_accepts_schema_alias(env: Env) -> None:
    node = Node(env)
    raw = node.hello().model_dump(mode="json", by_alias=True)
    assert "schema" in raw and "schema_" not in raw
    r = node.raw("POST", "/sessions", json=raw)
    assert r.status_code == 200 and r.json()["session_id"].startswith("rse_")


def test_push_url_and_detail(env: Env) -> None:
    api, node = env.api, Node(env)
    node.boot()
    url = f"/api/v1/runners/{node.id}/push-url"
    for bad in ("ftp://h/x", "http://u:p@h/x", "http://h/x#frag", "http:///x", "nonsense"):
        assert api.raw("PUT", url, json={"url": bad}).status_code == 422, bad
    ok = api.raw("PUT", url, json={"url": "https://runner.local:9000/"})
    assert ok.status_code == 200 and ok.json()["push_url"] == "https://runner.local:9000/"
    assert api.raw("PUT", url, json={"url": None}).json()["push_url"] is None
    assert api.raw("PUT", "/api/v1/runners/rnr_nope/push-url", json={"url": None}).status_code == 404
    node.lease()
    detail = api.get(f"/api/v1/runners/{node.id}")
    assert detail["attempts"][0]["state"] == "leased" and detail["attempt_counts"] == {"leased": 1}
    assert {a["event"] for a in detail["audit"]} >= {"register", "inventory", "push_url_set"}
    assert api.c.get("/api/v1/runners/rnr_nope").status_code == 404


# --- end to end with the real agent ---------------------------------------------------------------------------------
def test_agent_end_to_end(env: Env, tmp_path: Path) -> None:
    api, studio = env.api, env.api.studio
    token = reg_token(api, make_group(api))
    cfg = RunnerConfig.model_validate({
        "studio_url": BASE, "name": "e2e", "state_dir": str(tmp_path / "state"), "host_lock": str(tmp_path / "lock"),
        "dispatch": "pull", "slots": [{"slot_id": "aux3d-0", "capability": "aux3d", "devices": ["GPU-e2e-1"],
                                       "engines": ["aux", "worker3d"]}], "acquire_wait_s": 0, "simulated": True})
    now = [1000.0]
    agent = RunnerAgent(cfg, client=RunnerClient(BASE, private_key=load_or_create_key(cfg), http=api.c),
                        executor=FakeExecutor(), state=RunnerState(cfg.state_dir),
                        spool=Spool(cfg.state_dir / "spool"), clock=lambda: now[0], idle_s=0.0)
    agent.bootstrap(token)
    agent.open_session()
    offered = env.offer(b"agent input bytes")
    assert offered["runner_id"] is not None
    assert agent.step() is True
    row = studio.journal.attempts.get(offered["id"])
    assert row and row["state"] == "ingested"
    sha = row["manifest"]["files"][0]["sha256"]
    assert studio.registry.get(env.pid).store.repo.blob_exists(sha)
    assert claims(env, "GPU-e2e-1") == "free"
    (listed,) = api.get("/api/v1/runners")["runners"]
    assert listed["session"]["fresh"] is True and listed["attempt_counts"] == {"ingested": 1}
    assert attempts.commit(studio, offered["id"])
    now[0] += 1000  # heartbeat and receipt poll are due
    agent.step()
    assert agent.state.list_attempts() == [] and agent.spool.attempt_ids() == []
    assert studio.journal.attempts.get(offered["id"])["disposition"] == "committed"  # type: ignore[index]


def test_incompatible_protocol_is_flat_error(env: Env) -> None:
    node = Node(env)
    r = node.raw("POST", "/sessions", json=node.hello([99]).model_dump(mode="json", by_alias=True))
    assert r.status_code == 409 and r.json()["code"] == "protocol_incompatible" and "error" not in r.json()


def test_duplicate_accept_is_a_replay(env: Env) -> None:
    node = Node(env)
    node.boot()
    offer = node.lease()
    again = node.accept(offer)
    assert again.start_authorized is True
    events = [e["event"] for e in env.api.studio.journal.attempts.events(offer.attempt_id)]
    assert events.count("leased") == 1


def test_revoked_runner_is_refused(env: Env) -> None:
    node = Node(env)
    node.boot()
    api = env.api
    assert api.c.post(f"/api/v1/runners/{node.id}:revoke").status_code == 403
    assert api.post(f"/api/v1/runners/{node.id}:revoke")["state"] == "revoked"
    assert node.raw("POST", f"/sessions/{node.sid}/heartbeat", json={"session_id": node.sid,
                                                                     "inventory_revision": 0}).status_code == 401
    with pytest.raises(ApiError) as e:
        node.client.token()
    assert e.value.status == 401
    api.post("/api/v1/runners/rnr_nope:revoke", status=404)


def test_deregister_after_custody(env: Env) -> None:
    node = Node(env)
    node.boot()
    assert node.raw("DELETE", "/runners/self").status_code == 204
    assert node.raw("GET", "/attempts/atp_x/receipt").status_code == 401


def test_device_claimed_by_another_runner(env: Env) -> None:
    first, second = Node(env, "r1", "GPU-shared"), Node(env, "r2", "GPU-shared")
    first.boot()
    second.sid = second.client.open_session(second.hello()).session_id
    with pytest.raises(ApiError) as e:
        second.client.put_inventory(second.sid, second.inventory())
    assert e.value.status == 409 and e.value.code == "forbidden_scope"


# --- long poll -----------------------------------------------------------------------------------------------
def test_acquire_long_poll(env: Env) -> None:
    node = Node(env)
    node.boot()
    assert node.acquire(0) is None
    result: list[Offer | None] = []
    t0 = time.monotonic()
    th = threading.Thread(target=lambda: result.append(node.acquire(wait_s=3)))
    th.start()
    time.sleep(0.3)
    with pytest.raises(ApiError) as e:  # one outstanding acquire per session
        node.acquire(0)
    assert e.value.status == 409 and e.value.code == "invalid_input"
    placed = env.offer()
    th.join(timeout=5)
    assert result and result[0] is not None and result[0].attempt_id == placed["id"]
    assert time.monotonic() - t0 < 2.0


# --- bytes ------------------------------------------------------------------------------------------------------
def test_input_download_ranges_and_scope(env: Env) -> None:
    data = b"0123456789abcdef"
    node, other = Node(env, "r1"), Node(env, "r2")
    node.boot()
    offer = node.lease(data=data)
    sha, path = offer.inputs[0].sha256, f"/attempts/{offer.attempt_id}/inputs/"
    full = node.raw("GET", path + sha)
    assert full.status_code == 200 and full.content == data and hashlib.sha256(full.content).hexdigest() == sha
    assert full.headers["content-length"] == "16" and full.headers["accept-ranges"] == "bytes"
    part = node.raw("GET", path + sha, headers={"Range": "bytes=5-"})
    assert part.status_code == 206 and part.content == data[5:]
    assert part.headers["content-range"] == "bytes 5-15/16" and part.headers["content-length"] == "11"
    for bad in ("bytes=0-3", "bytes=-4", "bytes=16-", "items=1-"):
        r = node.raw("GET", path + sha, headers={"Range": bad})
        assert r.status_code == 416 and r.json()["code"] == "invalid_input", bad
    unbound = transfers.stage_input(env.api.studio, b"not part of this attempt")
    assert node.raw("GET", path + unbound).json()["code"] == "forbidden_scope"
    assert other.raw("GET", path + sha).status_code == 403  # another runner's attempt


def test_chunk_upload_over_http(env: Env) -> None:
    node = Node(env)
    node.boot()
    offer = node.lease()
    node.report(offer, "executing")
    data = b"result bytes"
    sha = hashlib.sha256(data).hexdigest()
    up = node.client.create_upload(UploadCreate(attempt_id=offer.attempt_id, generation=1, sha256=sha,
                                                size=len(data), role="result.bin", mime="application/octet-stream"))
    url, hdr = f"/uploads/{up.upload_id}/chunks/0", {"X-Chunk-Sha256": sha}
    big = node.raw("PUT", url, content=b"x", headers={**hdr, "Content-Length": str(MAX_CHUNK + 1)})
    assert big.status_code == 413 and big.json()["code"] == "resource_exhausted"
    assert node.raw("PUT", url, content=data).status_code == 400  # X-Chunk-Sha256 is required
    assert node.raw("PUT", url, content=data, headers=hdr).json() == {"status": "stored"}
    assert node.raw("PUT", url, content=data, headers=hdr).json() == {"status": "duplicate"}
    other = b"RESULT BYTES"
    clash = node.raw("PUT", url, content=other, headers={"X-Chunk-Sha256": hashlib.sha256(other).hexdigest()})
    assert clash.status_code == 409
    assert node.client.upload_status(up.upload_id).received == [0]
    receipt = node.client.finalize_upload(up.upload_id)
    assert receipt.sha256 == sha and receipt.size == len(data)
    assert env.api.studio.registry.get(env.pid).store.repo.blob_exists(sha)


# --- lease expiry and operator recovery -------------------------------------------------------------------------
def test_lease_expiry_marks_uncertain_then_declare_lost(env: Env) -> None:
    api, studio = env.api, env.api.studio
    node = Node(env)
    node.boot()
    task = env.task()
    offer = node.lease(task=task)
    assert claims(env, node.device) == "reserved"
    assert attempts.expire(studio, now=now_dt() + timedelta(hours=1))["uncertain"] == 1
    assert claims(env, node.device) == "uncertain"
    (listed,) = api.get("/api/v1/runners")["runners"]
    assert listed["attempt_counts"] == {"uncertain": 1}
    assert [d["claim"] for d in listed["devices"]] == ["uncertain"]
    assert api.c.post(f"/api/v1/attempts/{offer.attempt_id}:declare-lost").status_code == 403
    lost = api.post(f"/api/v1/attempts/{offer.attempt_id}:declare-lost")
    assert lost["state"] == "lost"
    api.post(f"/api/v1/attempts/{offer.attempt_id}:declare-lost", status=409)
    api.post("/api/v1/attempts/atp_nope:declare-lost", status=404)
    again = env.offer(task=task)
    assert again["generation"] == 2 and again["id"] != offer.attempt_id
    audit = api.get(f"/api/v1/runners/{node.id}")["audit"]
    assert any(a["event"] == "declare_lost" and a["actor"] == "operator" for a in audit)


# --- maintenance loop ----------------------------------------------------------------------------------------------
def test_maintenance_steps_are_isolated(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    studio, ran = env.api.studio, []

    def boom(_: Any) -> None:
        raise RuntimeError("expire exploded")

    monkeypatch.setattr(attempts, "expire", boom)
    monkeypatch.setattr(attempts, "place_pending", lambda s: ran.append("place"))
    monkeypatch.setattr(transfers, "expire_uploads", lambda s: ran.append("uploads"))
    RunnerMaintenance(studio).run_once()
    assert ran == ["place", "uploads"]
    studio.settings.runner_maintenance_s = 0.01
    m = RunnerMaintenance(studio)
    m.start()
    deadline = time.monotonic() + 3
    while ran.count("place") < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    m.stop()
    assert ran.count("place") >= 3
