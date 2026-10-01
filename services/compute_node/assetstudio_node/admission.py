"""Explicit GPU ownership for a time-shared device. Unknown ownership blocks dispatch; it never implies release.

Fencing: every grant uses a new monotonic epoch (persisted by the caller, so a restarted Studio never reuses one).
The granted worker only admits requests carrying that epoch; every worker, the target included unless it already
acknowledged a release, must have drained (no queued or running GPU work) and released its weights for that epoch
before the grant happens.
"""
from __future__ import annotations

import secrets
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_protocol.engine import AckError


class OwnershipUnknown(Exception):
    code = "gpu_ownership_unknown"


@dataclass
class LaneWorker:
    lease: Callable[[int], dict[str, Any]]
    unload: Callable[[str, int], dict[str, Any]]


def _check_release(body: dict[str, Any], token: str, epoch: int) -> None:
    if not isinstance(body, dict) or body.get("loaded") is not False or body.get("owner_token") != token:
        raise AckError(f"unload not acknowledged: {str(body)[:200]}")
    if body.get("epoch") != epoch or body.get("active") != 0 or body.get("admitting") is not False:
        raise AckError(f"release not confirmed (epoch/active/admitting): {str(body)[:200]}")


@dataclass
class GpuLane:
    """Workers sharing one physical GPU."""

    name: str
    workers: dict[str, LaneWorker]
    next_epoch: Callable[[], int]
    owner: str | None = None
    state: str = "unknown"  # unknown until the first verified handoff: never assume a fresh start means idle
    epoch: int = 0
    token: str = field(default_factory=lambda: secrets.token_hex(8))
    sessions: dict[str, str] = field(default_factory=dict)
    last_error: str | None = None
    grants: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)
    released: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _event(self, event: str, **kw: Any) -> None:
        self.history = [*self.history[-50:], {"at": now_iso(), "event": event, **kw}]

    def _unknown(self, msg: str, worker: str) -> OwnershipUnknown:
        self.state, self.owner, self.last_error = "unknown", None, msg
        self._event("unknown", worker=worker, detail=msg[:200])
        return OwnershipUnknown(f"{self.name}: {msg}")

    def acquire(self, worker: str) -> int:
        """Grant `worker` the device; returns the epoch its requests must carry."""
        with self._lock:
            if self.owner == worker and self.state == "owned":
                return self.epoch
            epoch = self.next_epoch()
            # The target drains too unless it acknowledged a release since: after a restart its old requests may
            # still run, and a fresh grant must never overlap them.
            for other, w in self.workers.items():
                if other in self.released:
                    continue
                try:
                    _check_release(w.unload(self.token, epoch), self.token, epoch)
                except AckError as e:
                    raise self._unknown(f"{other} did not acknowledge release ({e})", other) from e
                self.released.add(other)
                self._event("released", worker=other, epoch=epoch)
            try:
                info = self.workers[worker].lease(epoch)
            except AckError as e:
                raise self._unknown(f"{worker} did not accept the lease ({e})", worker) from e
            if info.get("epoch") != epoch or info.get("admitting") is not True or not info.get("session_id"):
                raise self._unknown(f"{worker} lease reply is malformed: {str(info)[:200]}", worker)
            self.released.discard(worker)
            self.sessions[worker] = str(info["session_id"])
            self.owner, self.state, self.epoch, self.last_error = worker, "owned", epoch, None
            self.grants += 1
            self._event("granted", worker=worker, epoch=epoch, session=self.sessions[worker])
            return epoch

    def epoch_for(self, worker: str) -> int:
        with self._lock:
            if self.owner != worker or self.state != "owned":
                raise OwnershipUnknown(f"{self.name}: {worker} does not own the device")
            return self.epoch

    def reset(self) -> dict[str, Any]:
        """Operator recovery: every worker must acknowledge release before the lane is usable again."""
        with self._lock:
            epoch = self.next_epoch()
            errors = {}
            self.released.clear()
            for other, w in self.workers.items():
                try:
                    _check_release(w.unload(self.token, epoch), self.token, epoch)
                    self.released.add(other)
                except AckError as e:
                    errors[other] = str(e)
            if errors:
                self.state, self.owner, self.last_error = "unknown", None, "; ".join(errors.values())
                return {"ok": False, "errors": errors}
            self.state, self.owner, self.last_error, self.epoch = "released", None, None, epoch
            self._event("reset", epoch=epoch)
            return {"ok": True}

    def public(self) -> dict[str, Any]:
        return {"lane": self.name, "owner": self.owner, "state": self.state, "last_error": self.last_error,
                "workers": sorted(self.workers), "grants": self.grants, "epoch": self.epoch,
                "sessions": dict(self.sessions), "history": self.history[-10:]}


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
