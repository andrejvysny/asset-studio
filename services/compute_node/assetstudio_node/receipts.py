"""Model receipts (R10): full-hash verification of the catalog models this runner's engines use."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_protocol.inventory import ModelReceipt
from pydantic import TypeAdapter

from .config import RunnerConfig
from .models import HashCache, ModelStatus, verify_model
from .state import RunnerState

_RECEIPTS = TypeAdapter(list[ModelReceipt])
_STATUS_MAP = {"ok": "ok", "missing": "missing", "incomplete": "missing", "pending_access": "missing",
               "unverified_hash": "unpinned"}


def lock_identity(spec: dict[str, Any]) -> str:
    """sha256 of the exact blob Studio's residency identity hashes, so files_sha256[:12] is the residency hash."""
    blob = json.dumps({"repo": spec.get("repo"), "revision": spec.get("revision"), "files": spec.get("files")},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _receipt(key: str, spec: dict[str, Any], status: str, catalog_sha: str) -> ModelReceipt:
    return ModelReceipt(key=key, revision=str(spec.get("revision", "?")), files_sha256=lock_identity(spec),
                        verification="full", status=status, catalog_sha256=catalog_sha,  # type: ignore[arg-type]
                        verified_at=now_iso())


def _status(result: ModelStatus) -> str:
    return _STATUS_MAP.get(result.status, "corrupt")


def model_receipts(catalog: dict[str, Any], catalog_sha: str, models_root: Path | None, services: set[str],
                   cache: HashCache | None, *, simulated: bool = False) -> list[ModelReceipt]:
    """One receipt per catalog model served by `services`. Simulated runners never touch the disk (SIMULATED)."""
    out: list[ModelReceipt] = []
    for key, spec in (catalog.get("models") or {}).items():
        if spec.get("service") not in services:
            continue
        if simulated:
            status = "ok"
        elif models_root is None:
            status = "missing"
        else:
            status = _status(verify_model(key, spec, models_root, full=True, cache=cache))
        out.append(_receipt(key, spec, status, catalog_sha))
    return out


def session_receipts(config: RunnerConfig, state: RunnerState, catalog: dict[str, Any],
                     catalog_sha: str) -> list[ModelReceipt]:
    """Verified once per catalog sha (full hashing is expensive); an all-ok result is persisted in the runner state."""
    cached = state.get_identity(f"receipts:{catalog_sha}")
    if cached:
        return _RECEIPTS.validate_json(cached)
    services = {e for sc in config.slots for e in sc.engines}
    cache = HashCache(config.state_dir / "hash-cache.json")
    out = model_receipts(catalog, catalog_sha, config.models_root, services, cache, simulated=config.simulated)
    if all(r.status == "ok" for r in out):  # a failed verdict must be re-checked once the operator fixes the files
        state.set_identity(f"receipts:{catalog_sha}", _RECEIPTS.dump_json(out).decode())
    return out
