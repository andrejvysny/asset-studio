"""Runner protocol over HTTP (docs/modular/compute-runner.md R17). Thin wrappers over `services`: bearer auth,
DTO parsing and byte streaming live here, every decision lives in the service layer. Errors are flat ErrorBody."""
from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import Iterator
from typing import Any, BinaryIO

from assetstudio_protocol.execution import (
    AcceptRequest,
    AcceptResponse,
    AcquireRequest,
    CompleteRequest,
    DispositionReceipt,
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
)
from assetstudio_protocol.transfer import (
    MAX_CHUNK,
    ChunkAck,
    IngestReceipt,
    UploadCreate,
    UploadCreated,
    UploadStatus,
)
from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from ..runner_errors import RunnerError
from ..services import attempts, runners, transfers
from ..services._runner_util import load_attempt
from ..studio import Studio
from .deps import studio

router = APIRouter(prefix="/api/runner/v1")
POLL_S = 0.25
_acquiring: set[str] = set()  # sessions with an outstanding long-poll; single event loop, so no lock
_RANGE = re.compile(r"^bytes=(\d+)-$")
_BLOCK = 1 << 20


class _Slot:
    """One transfer slot, released exactly once however the request ends."""

    def __init__(self, sem: threading.BoundedSemaphore) -> None:
        self._sem, self._lock, self._held = sem, threading.Lock(), True

    def release(self) -> None:
        with self._lock:
            held, self._held = self._held, False
        if held:
            self._sem.release()


def take_transfer_slot(s: Studio) -> _Slot | None:
    """R14: bulk transfers share a bounded pool; control routes (heartbeat, accept, acquire) never take a slot and
    a transfer that finds the pool empty is refused at once, never queued behind the event loop."""
    sem = s.extras.get("transfer_slots")
    if sem is None:
        sem = s.extras.setdefault("transfer_slots", threading.BoundedSemaphore(max(1, s.settings.transfer_concurrency)))
    return _Slot(sem) if sem.acquire(blocking=False) else None


def _busy() -> JSONResponse:
    body = RunnerError(503, "resource_exhausted", "transfer capacity exhausted; retry shortly").body()
    return JSONResponse(body, status_code=503, headers={"Retry-After": "1"})


def authed_runner(request: Request) -> dict[str, Any]:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise RunnerError(401, "unauthorized", "missing or malformed bearer token")
    return runners.authenticate(request.app.state.studio, token.strip())


Runner = Depends(authed_runner)
Stu = Depends(studio)


# --- unauthenticated ----------------------------------------------------------------------------------------------
@router.post("/register", response_model=RegisterResponse)
def register(req: RegisterRequest, s: Studio = Stu) -> RegisterResponse:
    return runners.register(s, req)


@router.post("/token/challenge", response_model=Challenge)
def challenge(req: ChallengeRequest, s: Studio = Stu) -> Challenge:
    return runners.challenge(s, req)


@router.post("/token", response_model=AccessToken)
def token(req: TokenRequest, s: Studio = Stu) -> AccessToken:
    return runners.issue_token(s, req)


# --- sessions -----------------------------------------------------------------------------------------------------
@router.post("/sessions", response_model=SessionAccepted)
def open_session(hello: SessionHello, runner: dict[str, Any] = Runner, s: Studio = Stu) -> SessionAccepted:
    return runners.open_session(s, runner, hello)


@router.put("/sessions/{sid}/inventory", response_model=InventoryAck)
def put_inventory(sid: str, inv: Inventory, runner: dict[str, Any] = Runner, s: Studio = Stu) -> InventoryAck:
    return runners.put_inventory(s, runner, sid, inv)


@router.post("/sessions/{sid}/heartbeat", response_model=HeartbeatResponse)
def heartbeat(sid: str, hb: Heartbeat, runner: dict[str, Any] = Runner, s: Studio = Stu) -> HeartbeatResponse:
    return attempts.heartbeat(s, runner, sid, hb)


@router.post("/sessions/{sid}/acquire", response_model=None)
async def acquire(sid: str, req: AcquireRequest, runner: dict[str, Any] = Runner, s: Studio = Stu) -> Response:
    if sid in _acquiring:
        raise RunnerError(409, "invalid_input", "an acquire is already outstanding for this session")
    _acquiring.add(sid)
    try:
        deadline = time.monotonic() + req.wait_s
        while True:
            offer = await run_in_threadpool(attempts.acquire, s, runner, sid, req)
            if offer is not None:
                return JSONResponse(offer.model_dump(mode="json"))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return Response(status_code=204)
            await asyncio.sleep(min(POLL_S, remaining))
    finally:
        _acquiring.discard(sid)


# --- attempts -----------------------------------------------------------------------------------------------------
@router.post("/attempts/{aid}/accept", response_model=AcceptResponse)
def accept(aid: str, req: AcceptRequest, runner: dict[str, Any] = Runner, s: Studio = Stu) -> AcceptResponse:
    return attempts.accept(s, runner, aid, req)


@router.post("/attempts/{aid}/reject", status_code=204)
def reject(aid: str, req: RejectRequest, runner: dict[str, Any] = Runner, s: Studio = Stu) -> Response:
    attempts.reject(s, runner, aid, req)
    return Response(status_code=204)


@router.post("/attempts/{aid}/report", response_model=ReportAck)
def report(aid: str, req: ReportRequest, runner: dict[str, Any] = Runner, s: Studio = Stu) -> ReportAck:
    return attempts.report(s, runner, aid, req)


@router.post("/attempts/{aid}/complete", response_model=ReportAck)
def complete(aid: str, req: CompleteRequest, runner: dict[str, Any] = Runner, s: Studio = Stu) -> ReportAck:
    return attempts.complete(s, runner, aid, req)


@router.get("/attempts/{aid}/receipt", response_model=DispositionReceipt)
def receipt(aid: str, runner: dict[str, Any] = Runner, s: Studio = Stu) -> DispositionReceipt:
    found = attempts.receipt(s, runner, aid)
    if found is None:
        raise RunnerError(404, "invalid_input", "no disposition yet")
    return found


def _blocks(f: BinaryIO, remaining: int, slot: _Slot) -> Iterator[bytes]:
    try:
        while remaining > 0:
            block = f.read(min(_BLOCK, remaining))
            if not block:
                return
            remaining -= len(block)
            yield block
    finally:
        f.close()
        slot.release()


def _open_input(s: Studio, runner: dict[str, Any], aid: str, sha: str) -> BinaryIO:
    attempt = load_attempt(s, runner, aid)
    if sha not in {i["sha256"] for i in (attempt["offer"] or {}).get("inputs", [])}:
        raise RunnerError(403, "forbidden_scope", "input is not bound to this attempt")
    return transfers.open_input(s, attempt["project_id"], sha)


def _range_start(header: str | None, size: int) -> int | None:
    """Only `bytes=N-` (resume from an offset) is supported; None means no Range header. Raises ValueError."""
    if header is None:
        return None
    m = _RANGE.match(header.strip())
    if m is None or int(m.group(1)) >= size:
        raise ValueError(header)
    return int(m.group(1))


@router.get("/attempts/{aid}/inputs/{sha}", response_model=None)
def get_input(aid: str, sha: str, range_header: str | None = Header(default=None, alias="range"),
              runner: dict[str, Any] = Runner, s: Studio = Stu) -> Response:
    slot = take_transfer_slot(s)
    if slot is None:
        return _busy()
    try:
        return _stream_input(s, runner, aid, sha, range_header, slot)
    except BaseException:
        slot.release()
        raise


def _stream_input(s: Studio, runner: dict[str, Any], aid: str, sha: str, range_header: str | None,
                  slot: _Slot) -> Response:
    f = _open_input(s, runner, aid, sha)
    size = f.seek(0, 2)
    try:
        start = _range_start(range_header, size)
    except ValueError:
        f.close()
        slot.release()
        err = RunnerError(416, "invalid_input", "only a satisfiable `bytes=N-` range is supported")
        return JSONResponse(err.body(), status_code=416, headers={"Content-Range": f"bytes */{size}"})
    f.seek(start or 0)
    headers = {"Content-Length": str(size - (start or 0)), "Accept-Ranges": "bytes"}
    if start is not None:
        headers["Content-Range"] = f"bytes {start}-{size - 1}/{size}"
    # The background task covers a response that never starts streaming (client gone before the first block).
    return StreamingResponse(_blocks(f, size - (start or 0), slot), status_code=200 if start is None else 206,
                             media_type="application/octet-stream", headers=headers,
                             background=BackgroundTask(slot.release))


# --- uploads ------------------------------------------------------------------------------------------------------
@router.post("/uploads", response_model=UploadCreated)
def create_upload(req: UploadCreate, runner: dict[str, Any] = Runner, s: Studio = Stu) -> UploadCreated:
    return transfers.create_upload(s, runner, req)


@router.put("/uploads/{uid}/chunks/{n}", response_model=ChunkAck)
async def put_chunk(uid: str, n: int, request: Request, x_chunk_sha256: str | None = Header(default=None),
                    runner: dict[str, Any] = Runner, s: Studio = Stu) -> ChunkAck | Response:
    declared = request.headers.get("content-length", "")
    if not declared.isdigit() or x_chunk_sha256 is None:
        raise RunnerError(400, "invalid_input", "Content-Length and X-Chunk-Sha256 are required")
    if int(declared) > MAX_CHUNK:
        raise RunnerError(413, "resource_exhausted", f"chunk exceeds {MAX_CHUNK} bytes")
    slot = take_transfer_slot(s)
    if slot is None:
        return _busy()
    try:
        data = await request.body()
        return await run_in_threadpool(transfers.put_chunk, s, runner, uid, n, [data], x_chunk_sha256, int(declared))
    finally:
        slot.release()


@router.get("/uploads/{uid}", response_model=UploadStatus)
def upload_status(uid: str, runner: dict[str, Any] = Runner, s: Studio = Stu) -> UploadStatus:
    return transfers.upload_status(s, runner, uid)


@router.post("/uploads/{uid}/finalize", response_model=IngestReceipt)
def finalize(uid: str, runner: dict[str, Any] = Runner, s: Studio = Stu) -> IngestReceipt:
    return transfers.finalize_upload(s, runner, uid)


@router.delete("/runners/self", status_code=204)
def deregister(runner: dict[str, Any] = Runner, s: Studio = Stu) -> Response:
    attempts.deregister(s, runner)
    return Response(status_code=204)
