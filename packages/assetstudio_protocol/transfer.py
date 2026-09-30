"""Chunked result upload DTOs (R9)."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import AttemptId, Msg, Sha256, Timestamp, UploadId

MIB = 1024 * 1024
MAX_CHUNK = 16 * MIB
DEFAULT_CHUNK = 8 * MIB
MAX_UPLOAD = 8 * 1024 * MIB


class UploadCreate(Msg):
    attempt_id: AttemptId
    generation: int = Field(ge=1)
    sha256: Sha256
    size: int = Field(ge=1, le=MAX_UPLOAD)
    role: str
    mime: str


class UploadCreated(Msg):
    upload_id: UploadId
    chunk_size: int = Field(ge=MIB, le=MAX_CHUNK)
    expires_at: Timestamp


class UploadStatus(Msg):
    upload_id: UploadId
    size: int
    chunk_size: int
    received: list[int]
    state: Literal["open", "finalizing", "finalized", "expired"]


class IngestReceipt(Msg):
    upload_id: UploadId
    sha256: Sha256
    size: int
    stored_at: Timestamp


def chunk_count(size: int, chunk_size: int) -> int:
    return -(-size // chunk_size)


def chunk_range(index: int, size: int, chunk_size: int) -> tuple[int, int]:
    """(start, end exclusive) byte range of chunk `index`."""
    if index < 0 or index >= chunk_count(size, chunk_size):
        raise ValueError(f"chunk index {index} out of range for size {size}")
    start = index * chunk_size
    return start, min(start + chunk_size, size)
