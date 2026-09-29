"""Local-folder backend: atomic durable writes, create-if-absent via link(), content-addressed blobs."""
from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
import threading
from pathlib import Path
from typing import BinaryIO

from .repo import BlobRef, Conflict, IntegrityError, NotFound, ObjectData, ReadOnly, StorageError, validate_key

_SHA_CHARS = set("0123456789abcdef")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _token(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalBackend:
    """One process owns writes (see owner.py); the per-key lock makes compare-and-replace atomic in-process."""

    def __init__(self, root: Path, read_only: bool = False) -> None:
        self.root = root.resolve()
        self.read_only = read_only
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        if not self.root.is_dir():
            raise StorageError(f"project root does not exist: {self.root}")

    def _path(self, key: str) -> Path:
        p = (self.root / validate_key(key)).resolve()
        if not p.is_relative_to(self.root):
            raise StorageError(f"key escapes project root: {key!r}")
        return p

    def _key_lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def _check_writable(self) -> None:
        if self.read_only:
            raise ReadOnly("project is open read-only")

    def _stage(self, directory: Path, data: bytes) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return Path(tmp)

    def read_object(self, key: str) -> ObjectData:
        p = self._path(key)
        try:
            data = p.read_bytes()
        except FileNotFoundError as e:
            raise NotFound(key) from e
        return ObjectData(data, _token(data))

    def create_if_absent(self, key: str, data: bytes) -> str:
        self._check_writable()
        p = self._path(key)
        tmp = self._stage(p.parent, data)
        try:
            os.link(tmp, p)  # atomic; fails if p exists
        except FileExistsError as e:
            raise Conflict(f"{key} already exists") from e
        finally:
            tmp.unlink(missing_ok=True)
        _fsync_dir(p.parent)
        return _token(data)

    def replace_if_version(self, key: str, expected_token: str, data: bytes) -> str:
        self._check_writable()
        p = self._path(key)
        with self._key_lock(key):
            try:
                current = _token(p.read_bytes())
            except FileNotFoundError as e:
                raise NotFound(key) from e
            if current != expected_token:
                raise Conflict(f"{key} changed since it was read")
            tmp = self._stage(p.parent, data)
            os.replace(tmp, p)
            _fsync_dir(p.parent)
        return _token(data)

    def delete_object(self, key: str) -> None:
        self._check_writable()
        try:
            self._path(key).unlink()
        except FileNotFoundError as e:
            raise NotFound(key) from e

    def list_keys(self, prefix: str, cursor: str | None = None, limit: int = 1000) -> tuple[list[str], str | None]:
        base = self._path(prefix.rstrip("/")) if prefix.strip("/") else self.root
        if not base.is_dir():
            return [], None
        keys = sorted(
            str(p.relative_to(self.root)) for p in base.rglob("*")
            if p.is_file() and not p.name.startswith(".tmp-")
        )
        if cursor:
            keys = [k for k in keys if k > cursor]
        page = keys[:limit]
        return page, (page[-1] if len(keys) > limit else None)

    def stat_object(self, key: str) -> dict | None:
        p = self._path(key)
        if not p.is_file():
            return None
        st = p.stat()
        return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}

    # --- blobs -------------------------------------------------------------------------------------------------
    def _blob_path(self, sha256: str) -> Path:
        if len(sha256) != 64 or not set(sha256) <= _SHA_CHARS:
            raise StorageError(f"invalid sha256 {sha256!r}")
        return self.root / "blobs" / "sha256" / sha256[:2] / sha256[2:4] / sha256

    def write_blob(self, stream: BinaryIO, expected_sha256: str | None = None) -> BlobRef:
        self._check_writable()
        staging = self.root / "blobs" / ".staging"
        staging.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        size = 0
        fd, tmp_name = tempfile.mkstemp(dir=staging, prefix=".tmp-")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    h.update(chunk)
                    size += len(chunk)
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            sha = h.hexdigest()
            if expected_sha256 is not None and sha != expected_sha256:
                raise IntegrityError(f"blob hash {sha} != expected {expected_sha256}")
            final = self._blob_path(sha)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(tmp, 0o444)
            try:
                os.link(tmp, final)
                created = True
                _fsync_dir(final.parent)
            except FileExistsError:
                created = False
            return BlobRef(sha, size, created)
        finally:
            tmp.unlink(missing_ok=True)

    def blob_exists(self, sha256: str) -> bool:
        return self._blob_path(sha256).is_file()

    def blob_size(self, sha256: str) -> int:
        try:
            return self._blob_path(sha256).stat().st_size
        except FileNotFoundError as e:
            raise NotFound(f"blob {sha256}") from e

    def open_blob(self, sha256: str) -> BinaryIO:
        try:
            return self._blob_path(sha256).open("rb")
        except FileNotFoundError as e:
            raise NotFound(f"blob {sha256}") from e

    def blob_path(self, sha256: str) -> Path | None:
        """Local-only fast path for range streaming; other backends return None."""
        p = self._blob_path(sha256)
        return p if p.is_file() else None

    def iter_blobs(self) -> list[tuple[str, int]]:
        base = self.root / "blobs" / "sha256"
        if not base.is_dir():
            return []
        return [(p.name, p.stat().st_size) for p in base.glob("*/*/*") if p.is_file()]

    def describe(self) -> dict:
        return {"backend": "local", "root": str(self.root), "read_only": self.read_only}


class WriterLock:
    """Exclusive project writer lock (flock) held for the Studio process lifetime. Same-host only."""

    def __init__(self, root: Path) -> None:
        self.path = root / "_control" / "writer.lock"
        self._fd: int | None = None

    def try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
