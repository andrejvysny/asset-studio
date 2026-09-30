"""RunnerClient and key utilities against httpx.MockTransport (no server)."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from assetstudio_client import ApiError, RunnerClient, TransportError, keys
from assetstudio_core.ids import new_id
from assetstudio_protocol.execution import (
    AcceptRequest,
    AcquireRequest,
    InputRef,
    Offer,
    Policy,
    Requirements,
    compute_input_digest,
)
from assetstudio_protocol.runners import StudioKey, challenge_message
from assetstudio_protocol.transfer import MIB

RID = new_id("rnr")
SID = new_id("rse")
AID = new_id("atp")
UID = new_id("xfr")
BASE = "http://studio.test"
API = "/api/runner/v1"
SERVER_AUD = "studio-audience-from-server"
NONCE = "n" * 40


def iso(delta_s: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=delta_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_offer() -> Offer:
    inputs = [InputRef(sha256="a" * 64, size=3, role="source", mime="image/png")]
    params = {"prompt": "x"}
    req = Requirements(capability="image", engine="comfyui", models=["m@abc123def456"])
    return Offer(schema="assetstudio.execution.v1", attempt_id=AID, task_id="t", call_key="j/1", generation=1,
                 runner_id=RID, session_id=SID, slot_id="gpu0", operation="image.edit", operation_version=1,
                 input_digest=compute_input_digest("image.edit", 1, inputs, params, req, Policy()), inputs=inputs,
                 params=params, requirements=req, offer_expires_at="2026-01-01T00:00:00Z")


class Server:
    """Mock Studio: handles the token dance, delegates other routes to `routes`."""

    def __init__(self, public_b64: str, token_ttl: float = 3600) -> None:
        self.public_b64 = public_b64
        self.token_ttl = token_ttl
        self.tokens_issued = 0
        self.calls: list[httpx.Request] = []
        self.reject_next_auth = 0
        self.routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {}
        self.token_sig_ok: bool | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path.removeprefix(API)
        if path == "/token/challenge":
            return httpx.Response(200, json={"nonce": NONCE, "expires_at": iso(60), "audience": SERVER_AUD})
        if path == "/token":
            body = json.loads(request.content)
            self.token_sig_ok = keys.verify(self.public_b64, challenge_message(RID, NONCE, SERVER_AUD),
                                            body["signature"])
            self.tokens_issued += 1
            return httpx.Response(200, json={"access_token": f"tok{self.tokens_issued}",
                                             "expires_at": iso(self.token_ttl)})
        if self.reject_next_auth:
            self.reject_next_auth -= 1
            return httpx.Response(401, json={"code": "unauthorized", "message": "expired"})
        handler = self.routes.get((request.method, path))
        if handler is None:
            return httpx.Response(404, json={"code": "invalid_input", "message": f"no route {path}"})
        return handler(request)


def make_client(server: Server | Callable[[httpx.Request], httpx.Response], priv: bytes | None = None,
                runner_id: str | None = RID) -> RunnerClient:
    return RunnerClient(BASE, private_key=priv, runner_id=runner_id,
                        http=httpx.Client(transport=httpx.MockTransport(server)))


@pytest.fixture
def priv() -> bytes:
    return keys.generate_private_key()


@pytest.fixture
def server(priv: bytes) -> Server:
    return Server(keys.public_key_b64(priv))


# -- keys ----------------------------------------------------------------------------------------------------------


def test_sign_verify_roundtrip(priv: bytes) -> None:
    assert len(priv) == 32
    pub = keys.public_key_b64(priv)
    assert len(base64.b64decode(pub)) == 32
    sig = keys.sign(priv, b"msg")
    assert len(base64.b64decode(sig)) == 64
    assert keys.verify(pub, b"msg", sig)
    assert not keys.verify(pub, b"other", sig)


def test_verify_never_raises_on_bad_input(priv: bytes) -> None:
    pub = keys.public_key_b64(priv)
    sig = keys.sign(priv, b"m")
    other = keys.public_key_b64(keys.generate_private_key())
    assert not keys.verify(other, b"m", sig)
    for bad_pub, bad_sig in (("!!!", sig), (pub, "!!!"), (pub, base64.b64encode(b"short").decode()),
                             (base64.b64encode(b"short").decode(), sig), ("", ""), (pub, "")):
        assert keys.verify(bad_pub, b"m", bad_sig) is False


def test_save_load_private_key(tmp_path: Path, priv: bytes) -> None:
    p = tmp_path / "runner.key"
    keys.save_private_key(p, priv)
    assert p.stat().st_mode & 0o777 == 0o600
    assert keys.load_private_key(p) == priv
    with pytest.raises(FileExistsError):
        keys.save_private_key(p, keys.generate_private_key())
    assert keys.load_private_key(p) == priv
    assert [f.name for f in tmp_path.iterdir()] == ["runner.key"]


def test_load_refuses_loose_mode_and_bad_length(tmp_path: Path, priv: bytes) -> None:
    p = tmp_path / "k"
    p.write_bytes(priv)
    os.chmod(p, 0o644)
    with pytest.raises(PermissionError, match="group/other"):
        keys.load_private_key(p)
    os.chmod(p, 0o600)
    p.write_bytes(priv[:10])
    with pytest.raises(ValueError, match="32 bytes"):
        keys.load_private_key(p)


def test_sign_verify_offer(priv: bytes) -> None:
    other = keys.generate_private_key()
    offer = keys.sign_offer(priv, make_offer())
    assert offer.signature
    cur = StudioKey(key_id="k1", public_key=keys.public_key_b64(priv), status="current")
    nxt = StudioKey(key_id="k2", public_key=keys.public_key_b64(priv), status="next")
    unrelated = StudioKey(key_id="k3", public_key=keys.public_key_b64(other), status="current")
    assert keys.verify_offer(offer, [cur])
    assert keys.verify_offer(offer, [unrelated, nxt])
    assert not keys.verify_offer(offer, [unrelated])
    assert not keys.verify_offer(make_offer(), [cur])  # unsigned
    tampered = offer.model_copy(update={"slot_id": "gpu1"})
    assert not keys.verify_offer(tampered, [cur])


# -- auth ----------------------------------------------------------------------------------------------------------


def test_register_stores_runner_id(priv: bytes) -> None:
    body = {"runner_id": RID, "group_id": new_id("rgp"), "ephemeral": False,
            "studio_keys": [{"key_id": "k", "public_key": keys.public_key_b64(priv), "status": "current"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"{API}/register"
        assert "authorization" not in request.headers
        assert json.loads(request.content)["schema"] == "assetstudio.runner.register.v1"
        return httpx.Response(200, json=body)

    from assetstudio_protocol.runners import RegisterRequest
    c = make_client(handler, priv, runner_id=None)
    out = c.register(RegisterRequest(schema="assetstudio.runner.register.v1", registration_token="t" * 20,
                                     public_key=keys.public_key_b64(priv), name="r",
                                     platform={"os": "linux", "arch": "x", "hostname": "h"}))
    assert out.runner_id == RID and c.runner_id == RID


def test_token_signs_server_audience(server: Server, priv: bytes) -> None:
    c = make_client(server, priv)
    assert c.audience == BASE  # client's own audience differs from what the server sent
    tok = c.token()
    assert tok.access_token == "tok1"
    assert server.token_sig_ok is True


def test_lazy_token_and_single_401_refresh(server: Server, priv: bytes) -> None:
    server.routes[("GET", f"/uploads/{UID}")] = lambda r: httpx.Response(
        200, json={"upload_id": UID, "size": 5, "chunk_size": MIB, "received": [], "state": "open"})
    c = make_client(server, priv)
    assert server.tokens_issued == 0
    c.upload_status(UID)
    assert server.tokens_issued == 1
    c.upload_status(UID)
    assert server.tokens_issued == 1  # cached
    server.reject_next_auth = 1
    c.upload_status(UID)
    assert server.tokens_issued == 2
    assert server.calls[-1].headers["authorization"] == "Bearer tok2"
    server.reject_next_auth = 5  # persistent 401: one refresh, then ApiError, no loop
    before = server.tokens_issued
    with pytest.raises(ApiError) as ei:
        c.upload_status(UID)
    assert ei.value.status == 401 and ei.value.code == "unauthorized"
    assert server.tokens_issued == before + 1


def test_token_refreshed_when_near_expiry(priv: bytes) -> None:
    srv = Server(keys.public_key_b64(priv), token_ttl=30)
    srv.routes[("DELETE", "/runners/self")] = lambda r: httpx.Response(204)
    c = make_client(srv, priv)
    c.deregister()
    c.deregister()
    assert srv.tokens_issued == 2  # 30 s left < 60 s margin


# -- calls ---------------------------------------------------------------------------------------------------------


def test_acquire_204_and_timeout(server: Server, priv: bytes) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions["timeout"]["read"]
        return httpx.Response(204)

    server.routes[("POST", f"/sessions/{SID}/acquire")] = handler
    c = make_client(server, priv)
    assert c.acquire(SID, AcquireRequest(request_id=str(uuid.uuid4()), wait_s=20)) is None
    assert seen["timeout"] == 35.0


def test_acquire_returns_offer(server: Server, priv: bytes) -> None:
    offer = make_offer()
    server.routes[("POST", f"/sessions/{SID}/acquire")] = lambda r: httpx.Response(200, json=offer.model_dump(mode="json"))
    c = make_client(server, priv)
    assert c.acquire(SID, AcquireRequest(request_id=str(uuid.uuid4()))) == offer


def test_receipt_404_is_none(server: Server, priv: bytes) -> None:
    server.routes[("GET", f"/attempts/{AID}/receipt")] = lambda r: httpx.Response(
        404, json={"code": "missing_artifact", "message": "none"})
    assert make_client(server, priv).receipt(AID) is None


def test_api_error_parsing(server: Server, priv: bytes) -> None:
    server.routes[("POST", f"/attempts/{AID}/accept")] = lambda r: httpx.Response(
        409, json={"code": "stale_generation", "message": "newer gen", "detail": {"current": 2}})
    c = make_client(server, priv)
    with pytest.raises(ApiError) as ei:
        c.accept(AID, AcceptRequest(session_id=SID, generation=1))
    assert (ei.value.status, ei.value.code, ei.value.message) == (409, "stale_generation", "newer gen")
    assert ei.value.detail == {"current": 2}

    server.routes[("POST", f"/attempts/{AID}/accept")] = lambda r: httpx.Response(502, text="<html>bad gateway")
    with pytest.raises(ApiError) as ei:
        c.accept(AID, AcceptRequest(session_id=SID, generation=1))
    assert ei.value.status == 502 and ei.value.code == "http_error"


def test_transport_error(priv: bytes) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    c = make_client(boom, priv)
    with pytest.raises(TransportError):
        c.challenge()
    with pytest.raises(TransportError):
        c.token()


# -- bytes ---------------------------------------------------------------------------------------------------------


def test_download_verifies_sha_and_resumes(server: Server, priv: bytes) -> None:
    data = b"hello world" * 100
    digest = hashlib.sha256(data).hexdigest()
    ranges: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ranges.append(request.headers.get("range"))
        if "range" in request.headers:
            start = int(request.headers["range"].removeprefix("bytes=").removesuffix("-"))
            return httpx.Response(206, content=data[start:])
        return httpx.Response(200, content=data)

    server.routes[("GET", f"/attempts/{AID}/inputs/{digest}")] = handler
    c = make_client(server, priv)
    out = io.BytesIO()
    assert c.download_input(AID, digest, out) == len(data)
    assert out.getvalue() == data
    tail = io.BytesIO()
    assert c.download_input(AID, digest, tail, start=500) == len(data) - 500
    assert tail.getvalue() == data[500:]
    assert ranges == [None, "bytes=500-"]
    bad = "b" * 64  # the server serves `data` under a hash it does not match
    server.routes[("GET", f"/attempts/{AID}/inputs/{bad}")] = handler
    with pytest.raises(ValueError, match="sha256 mismatch"):
        c.download_input(AID, bad, io.BytesIO())


def test_download_api_error(server: Server, priv: bytes) -> None:
    server.routes[("GET", f"/attempts/{AID}/inputs/{'a' * 64}")] = lambda r: httpx.Response(
        403, json={"code": "forbidden_scope", "message": "not bound"})
    with pytest.raises(ApiError) as ei:
        make_client(server, priv).download_input(AID, "a" * 64, io.BytesIO())
    assert ei.value.code == "forbidden_scope"


def test_upload_file_resume_sends_only_missing_chunks(server: Server, priv: bytes, tmp_path: Path) -> None:
    data = os.urandom(MIB * 2 + 10)  # chunks 0,1 full + chunk 2 of 10 bytes
    f = tmp_path / "out.bin"
    f.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    sent: dict[int, tuple[bytes, str, str]] = {}

    def create(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert (body["sha256"], body["size"], body["role"]) == (digest, len(data), "result")
        return httpx.Response(200, json={"upload_id": UID, "chunk_size": MIB, "expires_at": iso(600)})

    def chunk(request: httpx.Request) -> httpx.Response:
        idx = int(request.url.path.rsplit("/", 1)[1])
        sent[idx] = (request.content, request.headers["x-chunk-sha256"], request.headers["content-length"])
        return httpx.Response(200, json={"status": "stored"})

    server.routes[("POST", "/uploads")] = create
    server.routes[("GET", f"/uploads/{UID}")] = lambda r: httpx.Response(
        200, json={"upload_id": UID, "size": len(data), "chunk_size": MIB, "received": [0], "state": "open"})
    server.routes[("POST", f"/uploads/{UID}/finalize")] = lambda r: httpx.Response(
        200, json={"upload_id": UID, "sha256": digest, "size": len(data), "stored_at": iso(0)})
    for i in (1, 2):
        server.routes[("PUT", f"/uploads/{UID}/chunks/{i}")] = chunk
    receipt = make_client(server, priv).upload_file(f, attempt_id=AID, generation=1, role="result",
                                                    mime="application/octet-stream")
    assert receipt.sha256 == digest
    assert sorted(sent) == [1, 2]
    for i, (body, h, length) in sent.items():
        expected = data[i * MIB:(i + 1) * MIB]
        assert body == expected and h == hashlib.sha256(expected).hexdigest() and int(length) == len(expected)


def test_no_auto_retry_on_transport_error(server: Server, priv: bytes) -> None:
    n = {"c": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        n["c"] += 1
        raise httpx.ReadTimeout("slow")

    server.routes[("POST", f"/attempts/{AID}/accept")] = flaky
    c = make_client(server, priv)
    t0 = time.time()
    with pytest.raises(TransportError):
        c.accept(AID, AcceptRequest(session_id=SID, generation=1))
    assert n["c"] == 1 and time.time() - t0 < 5
