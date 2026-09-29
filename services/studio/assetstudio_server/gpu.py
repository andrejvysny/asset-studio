"""Explicit GPU ownership for a time-shared device. Unknown ownership blocks dispatch; it never implies release."""
from __future__ import annotations

import secrets
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from assetstudio_core.canonical import now_iso

from .adapters.base import AckError


class OwnershipUnknown(Exception):
    code = "gpu_ownership_unknown"


@dataclass
class GpuLane:
    """Workers sharing one physical GPU. `unloaders` must return only after an explicit, verified release ack."""

    name: str
    unloaders: dict[str, Callable[[str], dict[str, Any]]]
    owner: str | None = None
    state: str = "unknown"  # unknown until the first verified handoff: never assume a fresh start means idle
    token: str = field(default_factory=lambda: secrets.token_hex(8))
    last_error: str | None = None
    loads: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)
    released: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def acquire(self, worker: str) -> None:
        with self._lock:
            if self.owner == worker and self.state == "owned":
                return
            # Any worker not verified-released since startup may still hold memory: it must ack a release first.
            for other, unload in self.unloaders.items():
                if other == worker or other in self.released:
                    continue
                try:
                    unload(self.token)
                except AckError as e:
                    self.state, self.owner, self.last_error = "unknown", None, f"{other}: {e}"
                    self.history.append({"at": now_iso(), "event": "release_unacknowledged", "worker": other})
                    raise OwnershipUnknown(f"{self.name}: {other} did not acknowledge release ({e})") from e
                self.released.add(other)
            self.released.discard(worker)
            self.owner, self.state, self.last_error = worker, "owned", None
            self.loads += 1
            self.history = [*self.history[-50:], {"at": now_iso(), "event": "granted", "worker": worker}]

    def reset(self) -> dict[str, Any]:
        """Operator recovery: every worker must acknowledge release before the lane is usable again."""
        with self._lock:
            errors = {}
            self.released.clear()
            for other, unload in self.unloaders.items():
                try:
                    unload(self.token)
                    self.released.add(other)
                except AckError as e:
                    errors[other] = str(e)
            if errors:
                self.state, self.owner, self.last_error = "unknown", None, "; ".join(errors.values())
                return {"ok": False, "errors": errors}
            self.state, self.owner, self.last_error = "released", None, None
            return {"ok": True}

    def public(self) -> dict[str, Any]:
        return {"lane": self.name, "owner": self.owner, "state": self.state, "last_error": self.last_error,
                "workers": sorted(self.unloaders), "grants": self.loads}


def nvidia_smi() -> list[dict[str, Any]]:
    """Physical GPU facts; no CUDA context. Empty list when unavailable (library-only hosts)."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,uuid,name,memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in out.stdout.strip().splitlines():
        try:
            idx, uuid, name, used, total, util = [x.strip() for x in line.split(",")]
            gpus.append({"index": idx, "uuid": uuid, "name": name, "vram_used_mb": int(used),
                         "vram_total_mb": int(total), "util_pct": int(util), "measured_at": now_iso()})
        except ValueError:
            continue
    return gpus
