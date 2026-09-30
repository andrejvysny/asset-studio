"""Synchronous client for the runner API (docs/modular/compute-runner.md R17).

Retry policy: none for transport failures (the caller decides; a TransportError is an uncertain outcome). The single
automatic retry is one token refresh after a 401, which is safe because the server rejected the call before acting."""
from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, TypeVar

import httpx
from assetstudio_protocol.base import Msg
from assetstudio_protocol.execution import (
    AcceptRequest,
    AcceptResponse,
    AcquireRequest,
    CompleteRequest,
    DispositionReceipt,
    Offer,
    RejectRequest,
    ReportAck,
    ReportRequest,
)
from assetstudio_protocol.inventory import Inventory
from assetstudio_protocol.runners import (
    AccessToken,
    Challenge,
    ChallengeRequest,
    Heartbeat,
    HeartbeatResponse,
    InventoryAck,
    RegisterRequest,
    RegisterResponse,
    SessionAccepted,
    SessionHello,
    TokenRequest,
    challenge_message,
)
from assetstudio_protocol.transfer import ChunkAck, IngestReceipt, UploadCreate, UploadCreated, UploadStatus

from . import keys, transfer
from .errors import ApiError, TransportError

API_PREFIX = "/api/runner/v1"
REFRESH_MARGIN_S = 60.0
ACQUIRE_SLACK_S = 15.0
_M = TypeVar("_M", bound=Msg)


def _expiry_epoch(expires_at: str) -> float:
    dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()


class RunnerClient:
    def __init__(self, base_url: str, *, private_key: bytes | None = None, runner_id: str | None = None,
                 http: httpx.Client | None = None, timeout: float = 30.0, audience: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.runner_id = runner_id
        self.audience = audience or base_url
        self._private_key = private_key
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0))
        self._timeout = timeout
        self._token: str | None = None
        self._token_expiry = 0.0

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> RunnerClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- transport -----------------------------------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}{API_PREFIX}{path}"

    def _send(self, method: str, path: str, *, auth: bool, timeout: float | None = None, stream: bool = False,
              **kw: Any) -> httpx.Response:
        """One request, plus one retry after a token refresh on 401. Caller owns closing a streamed response."""
        for attempt in (0, 1):
            headers = {**kw.get("headers", {}), **(self._auth_headers() if auth else {})}
            try:
                req = self._http.build_request(method, self._url(path), **{**kw, "headers": headers},
                                               timeout=timeout if timeout is not None else self._timeout)
                resp = self._http.send(req, stream=stream)
            except httpx.TransportError as e:
                raise TransportError(f"{method} {path}: {type(e).__name__}: {e}") from e
            if resp.status_code == 401 and auth and attempt == 0:
                resp.close()
                self._token = None
                continue
            return resp
        raise AssertionError("unreachable")

    def _call(self, method: str, path: str, *, auth: bool = True, timeout: float | None = None,
              body: Msg | None = None, **kw: Any) -> httpx.Response:
        if body is not None:
            kw["json"] = body.model_dump(mode="json")
        resp = self._send(method, path, auth=auth, timeout=timeout, **kw)
        if resp.status_code >= 400:
            raise ApiError.from_response(resp)
        return resp

    def _parse(self, model: type[_M], resp: httpx.Response) -> _M:
        try:
            return model.model_validate_json(resp.content)
        except ValueError as e:
            raise ApiError(resp.status_code, "http_error", f"malformed {model.__name__} response: {e}") from e

    # -- authentication ------------------------------------------------------------------------------------------

    def register(self, req: RegisterRequest) -> RegisterResponse:
        out = self._parse(RegisterResponse, self._call("POST", "/register", auth=False, body=req))
        self.runner_id = out.runner_id
        return out

    def challenge(self) -> Challenge:
        if self.runner_id is None:
            raise RuntimeError("runner_id unset: register first")
        resp = self._call("POST", "/token/challenge", auth=False, body=ChallengeRequest(runner_id=self.runner_id))
        return self._parse(Challenge, resp)

    def token(self) -> AccessToken:
        if self.runner_id is None or self._private_key is None:
            raise RuntimeError("runner_id and private_key are required to obtain a token")
        ch = self.challenge()
        # Sign the audience the server sent, not our own notion of it.
        sig = keys.sign(self._private_key, challenge_message(self.runner_id, ch.nonce, ch.audience))
        resp = self._call("POST", "/token", auth=False,
                          body=TokenRequest(runner_id=self.runner_id, nonce=ch.nonce, signature=sig))
        tok = self._parse(AccessToken, resp)
        self._token = tok.access_token
        self._token_expiry = _expiry_epoch(tok.expires_at)
        return tok

    def _auth_headers(self) -> dict[str, str]:
        if self._token is None or self._token_expiry - time.time() < REFRESH_MARGIN_S:
            self.token()
        return {"Authorization": f"Bearer {self._token}"}

    # -- sessions and dispatch -----------------------------------------------------------------------------------

    def open_session(self, hello: SessionHello) -> SessionAccepted:
        return self._parse(SessionAccepted, self._call("POST", "/sessions", body=hello))

    def put_inventory(self, session_id: str, inv: Inventory) -> InventoryAck:
        return self._parse(InventoryAck, self._call("PUT", f"/sessions/{session_id}/inventory", body=inv))

    def heartbeat(self, session_id: str, hb: Heartbeat) -> HeartbeatResponse:
        return self._parse(HeartbeatResponse, self._call("POST", f"/sessions/{session_id}/heartbeat", body=hb))

    def acquire(self, session_id: str, req: AcquireRequest) -> Offer | None:
        resp = self._call("POST", f"/sessions/{session_id}/acquire", body=req,
                          timeout=req.wait_s + ACQUIRE_SLACK_S)
        return None if resp.status_code == 204 else self._parse(Offer, resp)

    def accept(self, attempt_id: str, req: AcceptRequest) -> AcceptResponse:
        return self._parse(AcceptResponse, self._call("POST", f"/attempts/{attempt_id}/accept", body=req))

    def reject(self, attempt_id: str, req: RejectRequest) -> None:
        self._call("POST", f"/attempts/{attempt_id}/reject", body=req)

    def report(self, attempt_id: str, req: ReportRequest) -> ReportAck:
        return self._parse(ReportAck, self._call("POST", f"/attempts/{attempt_id}/report", body=req))

    def complete(self, attempt_id: str, req: CompleteRequest) -> ReportAck:
        return self._parse(ReportAck, self._call("POST", f"/attempts/{attempt_id}/complete", body=req))

    def receipt(self, attempt_id: str) -> DispositionReceipt | None:
        resp = self._send("GET", f"/attempts/{attempt_id}/receipt", auth=True)
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise ApiError.from_response(resp)
        return self._parse(DispositionReceipt, resp)

    def deregister(self) -> None:
        self._call("DELETE", "/runners/self")

    # -- bytes ---------------------------------------------------------------------------------------------------

    def download_input(self, attempt_id: str, sha256: str, dst: BinaryIO, *, start: int = 0) -> int:
        """Stream an input into `dst`; returns bytes written. With start > 0 resumes via Range (the caller appends
        to a partial file and verifies the whole file itself). With start == 0 the sha256 is verified and a mismatch
        raises ValueError (after the bytes were written: the caller must discard `dst`)."""
        headers = {"Range": f"bytes={start}-"} if start > 0 else {}
        resp = self._send("GET", f"/attempts/{attempt_id}/inputs/{sha256}", auth=True, stream=True, headers=headers)
        try:
            if resp.status_code >= 400:
                resp.read()
                raise ApiError.from_response(resp)
            if start > 0 and resp.status_code != 206:
                raise ApiError(resp.status_code, "http_error", "server ignored Range request")
            h = hashlib.sha256()
            written = 0
            try:
                for block in resp.iter_bytes(1024 * 1024):
                    dst.write(block)
                    h.update(block)
                    written += len(block)
            except httpx.TransportError as e:
                raise TransportError(f"download {sha256}: {type(e).__name__}: {e}") from e
        finally:
            resp.close()
        if start == 0 and h.hexdigest() != sha256:
            raise ValueError(f"input sha256 mismatch: expected {sha256}, got {h.hexdigest()}")
        return written

    def create_upload(self, req: UploadCreate) -> UploadCreated:
        return self._parse(UploadCreated, self._call("POST", "/uploads", body=req))

    def put_chunk(self, upload_id: str, index: int, data: bytes) -> ChunkAck:
        headers = {"X-Chunk-Sha256": hashlib.sha256(data).hexdigest(), "Content-Type": "application/octet-stream"}
        resp = self._call("PUT", f"/uploads/{upload_id}/chunks/{index}", content=data, headers=headers)
        return self._parse(ChunkAck, resp)

    def upload_status(self, upload_id: str) -> UploadStatus:
        return self._parse(UploadStatus, self._call("GET", f"/uploads/{upload_id}"))

    def finalize_upload(self, upload_id: str) -> IngestReceipt:
        return self._parse(IngestReceipt, self._call("POST", f"/uploads/{upload_id}/finalize"))

    def upload_file(self, path: Path, *, attempt_id: str, generation: int, role: str, mime: str) -> IngestReceipt:
        return transfer.upload_file(self, path, attempt_id=attempt_id, generation=generation, role=role, mime=mime)
