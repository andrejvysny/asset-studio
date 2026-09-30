"""Input staging and resumable result uploads (R9, R14). Handlers stream; no whole file is held in memory."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any, BinaryIO

from assetstudio_protocol.transfer import (
    ChunkAck,
    IngestReceipt,
    UploadCreate,
    UploadCreated,
    UploadStatus,
    chunk_count,
    chunk_range,
)
from assetstudio_storage.repo import StorageError

from ..errors import ApiError
from ..runner_errors import RunnerError
from ..studio import Studio
from ._runner_util import after, fmt, load_attempt, now_dt

_UPLOADING = ("executing", "spooled", "uploading")
_FINALIZE_SLOTS = threading.BoundedSemaphore(2)
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_finalizing: set[str] = set()
_READ = 1 << 20


def _upload_lock(upload_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(upload_id, threading.Lock())


def _transfers(studio: Studio, *parts: str) -> Path:
    return studio.settings.instance_dir.joinpath("transfers", *parts)


def _part(studio: Studio, upload_id: str) -> Path:
    return _transfers(studio, "uploads", f"{upload_id}.part")


# --- inputs -------------------------------------------------------------------------------------------------------
def stage_input(studio: Studio, data: bytes) -> str:
    sha = hashlib.sha256(data).hexdigest()
    final = _transfers(studio, "inputs", sha[:2], sha)
    if final.is_file():
        return sha
    final.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=final.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, final)
        except FileExistsError:
            pass
    finally:
        Path(tmp).unlink(missing_ok=True)
    return sha


def open_input(studio: Studio, project_id: str, sha: str) -> BinaryIO:
    staged = _transfers(studio, "inputs", sha[:2], sha)
    if len(sha) == 64 and staged.is_file():
        return staged.open("rb")
    try:
        repo = studio.registry.get(project_id).store.repo
        if repo.blob_exists(sha):
            return repo.open_blob(sha)
    except (ApiError, StorageError):
        pass
    raise RunnerError(404, "missing_artifact", f"input {sha[:12]} is not available", {"sha256": sha})


# --- uploads ------------------------------------------------------------------------------------------------------
def _check_quotas(studio: Studio, runner: dict[str, Any], size: int) -> None:
    s, store = studio.settings, studio.journal.attempts
    free = shutil.disk_usage(s.instance_dir).free
    problem = None
    if store.reserved_bytes() + size > s.upload_quota_bytes:
        problem = "global upload quota exceeded"
    elif store.reserved_bytes(runner_id=runner["id"]) + size > s.upload_runner_quota_bytes:
        problem = "runner upload quota exceeded"
    elif free - size < s.disk_floor_bytes:
        problem = "disk reserve would be breached"
    if problem:
        raise RunnerError(507, "resource_exhausted", problem, {"size": size, "free_bytes": free})


def create_upload(studio: Studio, runner: dict[str, Any], req: UploadCreate) -> UploadCreated:
    attempt = load_attempt(studio, runner, req.attempt_id)
    if attempt["generation"] != req.generation:
        raise RunnerError(409, "stale_generation", "generation does not match the attempt")
    if attempt["state"] not in _UPLOADING:
        raise RunnerError(409, "invalid_input", f"attempt is {attempt['state']}, not accepting uploads")
    store = studio.journal.attempts
    existing = store.find_upload(attempt["id"], req.generation, req.sha256)
    if existing is None:
        _check_quotas(studio, runner, req.size)
    row, _ = store.create_upload(
        attempt_id=attempt["id"], generation=req.generation, project_id=attempt["project_id"],
        runner_id=runner["id"], sha256=req.sha256, size=req.size, role=req.role, mime=req.mime,
        chunk_size=studio.settings.upload_chunk_size, expires_at=after(studio.settings.upload_ttl_s))
    store.transition(attempt["id"], ("executing", "spooled"), "uploading", event="upload_started")
    return UploadCreated(upload_id=row["id"], chunk_size=row["chunk_size"], expires_at=row["expires_at"])


def _owned_upload(studio: Studio, runner: dict[str, Any], upload_id: str) -> dict[str, Any]:
    up = studio.journal.attempts.get_upload(upload_id)
    if up is None:
        raise RunnerError(404, "invalid_input", f"unknown upload {upload_id}")
    if up["runner_id"] != runner["id"]:
        raise RunnerError(403, "forbidden_scope", "upload belongs to another runner")
    return up


def _write_chunk(path: Path, offset: int, stream: Iterable[bytes], length: int) -> tuple[int, str]:
    h, n = hashlib.sha256(), 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "r+b" if path.exists() else "w+b") as f:
        f.seek(offset)
        for piece in stream:
            if n + len(piece) > length:
                raise RunnerError(400, "invalid_input", "chunk longer than Content-Length")
            h.update(piece)
            f.write(piece)
            n += len(piece)
        f.flush()
        os.fsync(f.fileno())
    return n, h.hexdigest()


def put_chunk(studio: Studio, runner: dict[str, Any], upload_id: str, index: int, stream: Iterable[bytes],
              chunk_sha256: str, content_length: int) -> ChunkAck:
    up = _owned_upload(studio, runner, upload_id)
    if up["state"] != "open":
        raise RunnerError(409, "invalid_input", f"upload is {up['state']}")
    try:
        start, end = chunk_range(index, up["size"], up["chunk_size"])
    except ValueError as e:
        raise RunnerError(400, "invalid_input", str(e)) from e
    if content_length != end - start:
        raise RunnerError(400, "invalid_input", f"chunk {index} must be {end - start} bytes")
    store = studio.journal.attempts
    with _upload_lock(upload_id):
        known = store.get_upload(upload_id) or up
        if (prior := known["received"].get(str(index))) is not None:
            for _ in stream:  # drain so the connection stays clean; nothing is written
                pass
            if prior != chunk_sha256:
                raise RunnerError(409, "invalid_input", "chunk conflict")
            return ChunkAck(status="duplicate")
        n, digest = _write_chunk(_part(studio, upload_id), start, stream, content_length)
        if n != content_length or digest != chunk_sha256:
            raise RunnerError(400, "invalid_input", "chunk bytes do not match Content-Length or X-Chunk-Sha256")
        outcome = store.record_chunk(upload_id, index, chunk_sha256)
    if outcome == "conflict":
        raise RunnerError(409, "invalid_input", "chunk conflict")
    if outcome == "closed":
        raise RunnerError(409, "invalid_input", "upload is no longer open")
    return ChunkAck(status="duplicate" if outcome == "duplicate" else "stored")


def upload_status(studio: Studio, runner: dict[str, Any], upload_id: str) -> UploadStatus:
    up = _owned_upload(studio, runner, upload_id)
    return UploadStatus(upload_id=upload_id, size=up["size"], chunk_size=up["chunk_size"],
                        received=sorted(int(i) for i in up["received"]), state=up["state"])


def _receipt(up: dict[str, Any]) -> IngestReceipt:
    return IngestReceipt(upload_id=up["id"], sha256=up["sha256"], size=up["size"],
                         stored_at=up["finalized_at"] or fmt(now_dt()))


def _hash_part(path: Path) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with path.open("rb") as f:
        for piece in iter(lambda: f.read(_READ), b""):
            h.update(piece)
            n += len(piece)
    return h.hexdigest(), n


def _begin_finalize(studio: Studio, up: dict[str, Any]) -> None:
    """Claims the finalize. 'finalizing' without an in-process owner is a crashed finalize and is taken over."""
    store = studio.journal.attempts
    with _locks_guard:
        if up["id"] in _finalizing:
            raise RunnerError(409, "invalid_input", "finalize already in progress")
        _finalizing.add(up["id"])
    if up["state"] == "open":
        if set(up["received"]) != {str(i) for i in range(chunk_count(up["size"], up["chunk_size"]))}:
            _end_finalize(up["id"])
            raise RunnerError(409, "invalid_input", "upload is missing chunks")
        if not store.set_upload_state(up["id"], ("open",), "finalizing"):
            _end_finalize(up["id"])
            raise RunnerError(409, "invalid_input", "upload is no longer open")


def _end_finalize(upload_id: str) -> None:
    with _locks_guard:
        _finalizing.discard(upload_id)


def finalize_upload(studio: Studio, runner: dict[str, Any], upload_id: str) -> IngestReceipt:
    up = _owned_upload(studio, runner, upload_id)
    if up["state"] == "finalized":
        return _receipt(up)
    if up["state"] == "expired":
        raise RunnerError(409, "invalid_input", "upload expired")
    _begin_finalize(studio, up)
    try:
        with _FINALIZE_SLOTS:
            return _verify_and_store(studio, up)
    finally:
        _end_finalize(upload_id)


def _verify_and_store(studio: Studio, up: dict[str, Any]) -> IngestReceipt:
    store, part = studio.journal.attempts, _part(studio, up["id"])
    digest, n = _hash_part(part) if part.is_file() else ("", -1)
    if digest != up["sha256"] or n != up["size"]:
        # The bytes can never become valid: a frozen upload is not reopened (R9), the runner must start over.
        store.set_upload_state(up["id"], ("finalizing",), "expired")
        part.unlink(missing_ok=True)
        raise RunnerError(400, "validation_failed", "uploaded bytes do not match the declared sha256/size")
    try:
        with part.open("rb") as f:
            studio.registry.get(up["project_id"]).store.repo.write_blob(f, expected_sha256=up["sha256"])
    except (ApiError, StorageError) as e:
        store.set_upload_state(up["id"], ("finalizing",), "open")
        raise RunnerError(503, "node_unavailable", "project storage could not take the blob") from e
    store.set_upload_state(up["id"], ("finalizing",), "finalized", finalized_at=fmt(now_dt()))
    part.unlink(missing_ok=True)
    return _receipt(store.get_upload(up["id"]) or up)


def expire_uploads(studio: Studio) -> int:
    n = 0
    for up in studio.journal.attempts.expired_uploads(fmt(now_dt())):
        if studio.journal.attempts.set_upload_state(up["id"], ("open", "finalizing"), "expired"):
            _part(studio, up["id"]).unlink(missing_ok=True)
            n += 1
    return n
