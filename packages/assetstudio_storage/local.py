"""Local-folder backend: atomic durable writes, create-if-absent via link(), content-addressed blobs."""
from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from .repo import (
    BlobRef,
    Conflict,
    CorruptBlob,
    IntegrityError,
    NotFound,
    ObjectData,
    ReadOnly,
    StorageError,
    validate_key,
)

_SHA_CHARS = set("0123456789abcdef")
_CHUNK = 1 << 20


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
        # Blobs are write-once (0444, never replaced): a verification stays valid while the file identity
        # (inode, size, mtime, ctime) is unchanged. Any rewrite/chmod/replace changes ctime and invalidates it.
        self._verified: dict[str, tuple[int, int, int, int]] = {}
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

    def scan_keys(self) -> Iterator[str]:
        for dirpath, dirnames, filenames in os.walk(self.root):
            if Path(dirpath) == self.root:
                dirnames[:] = [d for d in dirnames if d != "blobs"]
            for name in sorted(filenames):
                if not name.startswith(".tmp-"):
                    yield str((Path(dirpath) / name).relative_to(self.root))

    def delete_blob(self, sha256: str) -> bool:
        self._check_writable()
        p = self._blob_path(sha256)
        try:
            p.unlink()
        except FileNotFoundError:
            return False
        self._verified.pop(sha256, None)
        _fsync_dir(p.parent)
        return True

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
        """Content path, refusing symlinked components: a blob must never resolve outside the project root."""
        if len(sha256) != 64 or not set(sha256) <= _SHA_CHARS:
            raise StorageError(f"invalid sha256 {sha256!r}")
        p = self.root / "blobs" / "sha256" / sha256[:2] / sha256[2:4] / sha256
        cur = self.root
        for part in p.relative_to(self.root).parts:
            cur = cur / part
            if cur.is_symlink():
                raise IntegrityError(f"blob path component is a symlink: {cur.relative_to(self.root)}")
        return p

    @staticmethod
    def _identity(p: Path) -> tuple[int, int, int, int]:
        st = p.stat()
        return st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns

    def _hash_file(self, p: Path) -> tuple[str, int]:
        h, n = hashlib.sha256(), 0
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(_CHUNK), b""):
                h.update(chunk)
                n += len(chunk)
        return h.hexdigest(), n

    def verify_blob(self, sha256: str, size: int | None = None, use_cache: bool = True) -> None:
        """Raise NotFound / CorruptBlob unless the stored bytes hash to `sha256` (and have `size`)."""
        p = self._blob_path(sha256)
        if not p.is_file():
            raise NotFound(f"blob {sha256}")
        ident = self._identity(p)
        if use_cache and self._verified.get(sha256) == ident and (size is None or ident[1] == size):
            return
        digest, n = self._hash_file(p)
        if digest != sha256 or (size is not None and n != size):
            self._verified.pop(sha256, None)
            raise CorruptBlob(sha256, f"stored bytes hash to {digest[:12]} ({n} B)")
        self._verified[sha256] = ident

    def read_blob_verified(self, sha256: str, max_bytes: int | None = None) -> bytes:
        """Read + hash in one pass. Altered bytes are never returned under the original digest."""
        p = self._blob_path(sha256)
        try:
            f = p.open("rb")
        except FileNotFoundError as e:
            raise NotFound(f"blob {sha256}") from e
        h, parts, n = hashlib.sha256(), [], 0
        with f:
            for chunk in iter(lambda: f.read(_CHUNK), b""):
                n += len(chunk)
                if max_bytes is not None and n > max_bytes:
                    raise StorageError(f"blob {sha256[:12]} exceeds the {max_bytes} B read limit")
                h.update(chunk)
                parts.append(chunk)
        if h.hexdigest() != sha256:
            self._verified.pop(sha256, None)
            raise CorruptBlob(sha256, f"stored bytes hash to {h.hexdigest()[:12]}")
        return b"".join(parts)

    def copy_blob_verified(self, sha256: str, dst: BinaryIO) -> int:
        """Stream a blob into `dst`; raises CorruptBlob at the end if the bytes did not match (caller discards dst)."""
        p = self._blob_path(sha256)
        h, n = hashlib.sha256(), 0
        try:
            f = p.open("rb")
        except FileNotFoundError as e:
            raise NotFound(f"blob {sha256}") from e
        with f:
            for chunk in iter(lambda: f.read(_CHUNK), b""):
                h.update(chunk)
                dst.write(chunk)
                n += len(chunk)
        if h.hexdigest() != sha256:
            raise CorruptBlob(sha256, f"stored bytes hash to {h.hexdigest()[:12]}")
        return n

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
                self._verified[sha] = self._identity(final)
            except FileExistsError:
                # Dedup reuse: existence (or size) is not proof. A damaged existing blob is reported, never
                # silently reused and never overwritten (other records may reference it; repair is explicit).
                self.verify_blob(sha, size)
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
