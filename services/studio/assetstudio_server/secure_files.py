"""Private (0600) JSON files with crash-safe replacement. Shared by the MCP and integration credential stores."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private(path: Path, obj: object) -> None:
    """Atomic replace: unique tmp name (concurrent writers never share one), fsync file then directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")  # mkstemp creates 0600
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    _fsync_dir(path.parent)


def create_private_exclusive(path: Path, obj: object) -> bool:
    """Create-once write: False when the file already exists (another process won the race)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    _fsync_dir(path.parent)
    return True


@contextlib.contextmanager
def exclusive(path: Path) -> Iterator[None]:
    """Cross-process writer lock for `path`'s whole read-modify-write. Locks a separate, never-replaced
    `<name>.lock` file: a lock on the data file itself would be lost when write_private() replaces it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the flock
