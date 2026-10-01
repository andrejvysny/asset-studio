"""One agent per host (R7): an exclusive flock on a host-path file.

The lock proves only that one *agent* runs. Engines can outlive the agent, so it is necessary but not sufficient:
the engine recovery barrier (reconcile, unload at a new epoch) arrives in a later work package."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType


class HostLockBusy(RuntimeError):
    pass


class HostLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> HostLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            raise HostLockBusy(f"another runner agent holds the host lock {self.path}") from e
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())  # diagnostics only
        self._fd = fd
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
