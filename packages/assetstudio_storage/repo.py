"""Storage contract shared by every backend. Keys are logical; callers never see filesystem paths."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import BinaryIO, Protocol

_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$")


class StorageError(Exception):
    code = "storage_error"


class NotFound(StorageError):
    code = "not_found"


class Conflict(StorageError):
    """Expected-version mismatch or create over an existing key."""

    code = "conflict"


class ReadOnly(StorageError):
    code = "read_only"


class IntegrityError(StorageError):
    code = "integrity_error"


class CorruptBlob(IntegrityError):
    """Stored bytes no longer match their content address. Dependent work blocks; repair is explicit."""

    code = "artifact_corrupt"

    def __init__(self, sha256: str, detail: str = "") -> None:
        super().__init__(f"blob {sha256[:12]} is corrupt{': ' + detail if detail else ''}")
        self.sha256 = sha256


@dataclass(frozen=True)
class ObjectData:
    data: bytes
    token: str  # opaque concurrency token; NOT a content id for blobs


@dataclass(frozen=True)
class BlobRef:
    sha256: str
    size: int
    created: bool  # False when identical bytes were already stored (deduplicated)


def validate_key(key: str) -> str:
    if not _KEY_RE.fullmatch(key) or any(part in (".", "..") for part in key.split("/")):
        raise StorageError(f"invalid storage key {key!r}")
    return key


class Repository(Protocol):
    read_only: bool

    def read_object(self, key: str) -> ObjectData: ...

    def create_if_absent(self, key: str, data: bytes) -> str: ...

    def replace_if_version(self, key: str, expected_token: str, data: bytes) -> str: ...

    def list_keys(self, prefix: str, cursor: str | None = None, limit: int = 1000) -> tuple[list[str], str | None]: ...

    def stat_object(self, key: str) -> dict | None: ...

    def delete_object(self, key: str) -> None: ...

    def write_blob(self, stream: BinaryIO, expected_sha256: str | None = None) -> BlobRef: ...

    def blob_exists(self, sha256: str) -> bool: ...

    def blob_size(self, sha256: str) -> int: ...

    def open_blob(self, sha256: str) -> BinaryIO: ...

    def verify_blob(self, sha256: str, size: int | None = None, use_cache: bool = True) -> None: ...

    def read_blob_verified(self, sha256: str, max_bytes: int | None = None) -> bytes: ...

    def copy_blob_verified(self, sha256: str, dst: BinaryIO) -> int: ...

    def describe(self) -> dict: ...
