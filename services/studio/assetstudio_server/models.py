"""Model dependency closure: one verifier for CLI, runtime API and dispatch preflight.

Expectations come from config/models.lock.yaml (pinned upstream metadata), never from downloaded manifests.
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from assetstudio_core.safeyaml import load_yaml

Status = Literal["ok", "missing", "incomplete", "corrupt", "unverified_hash", "pending_access", "invalid_lock"]


@dataclass
class ModelStatus:
    key: str
    repo: str
    revision: str
    status: Status
    licence: str
    licence_status: str
    roles: list[str]
    optional: bool
    gated: bool
    detail: str = ""
    problems: list[str] = field(default_factory=list)
    bytes_expected: int = 0
    full_verified: bool = False

    @property
    def ready(self) -> bool:
        return self.status in ("ok", "unverified_hash")


def load_lock(config_dir: Path) -> dict[str, Any]:
    data = load_yaml((config_dir / "models.lock.yaml").read_bytes())
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not data.get("models"):
        raise ValueError("models.lock.yaml is empty or has an unsupported schema")
    return data


class HashCache:
    """sha256 per (resolved path, size, mtime_ns). Full verification reuses results only for unchanged files."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        try:
            self._data: dict[str, str] = json.loads(path.read_text())
        except (OSError, ValueError):
            self._data = {}

    def sha256(self, file: Path) -> str:
        st = file.stat()
        key = f"{file}|{st.st_size}|{st.st_mtime_ns}"
        with self._lock:
            if key in self._data:
                return self._data[key]
        h = hashlib.sha256()
        with file.open("rb") as f:
            for chunk in iter(lambda: f.read(8 << 20), b""):
                h.update(chunk)
        digest = h.hexdigest()
        with self._lock:
            self._data[key] = digest
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data))
        return digest


def verify_model(key: str, spec: dict[str, Any], models_root: Path, full: bool = False,
                 cache: HashCache | None = None) -> ModelStatus:
    files: dict[str, dict[str, Any]] = spec.get("files") or {}
    st = ModelStatus(key=key, repo=spec.get("repo", "?"), revision=str(spec.get("revision", "?")), status="ok",
                     licence=spec.get("licence", "unknown"), licence_status=spec.get("licence_status", "review"),
                     roles=list(spec.get("roles", [])), optional=bool(spec.get("optional")),
                     gated=bool(spec.get("gated")), bytes_expected=sum(int(f.get("size") or 0) for f in files.values()))
    if not files:
        st.status, st.detail = "invalid_lock", "lock lists no required files"
        return st
    base = (models_root / spec["local_dir"]).resolve()
    root = models_root.resolve()
    missing, wrong_size, bad_hash, no_hash = [], [], [], []
    for rel, meta in files.items():
        p = (base / rel).resolve()
        if not p.is_relative_to(root):
            st.problems.append(f"{rel}: resolves outside the models root")
            wrong_size.append(rel)
            continue
        if not p.is_file():
            missing.append(rel)
            continue
        if meta.get("size") is not None and p.stat().st_size != meta["size"]:
            wrong_size.append(rel)
            continue
        if meta.get("sha256") is None:
            no_hash.append(rel)
        elif full and (cache.sha256(p) if cache else hashlib.sha256(p.read_bytes()).hexdigest()) != meta["sha256"]:
            bad_hash.append(rel)
    pending = st.gated and all(f.get("sha256") is None for f in files.values())
    if missing and pending:
        st.status, st.detail = "pending_access", f"gated model not downloaded ({len(missing)} files missing)"
    elif missing:
        st.status, st.detail = "missing" if len(missing) == len(files) else "incomplete", \
            f"{len(missing)}/{len(files)} required files missing: {', '.join(missing[:3])}"
    elif wrong_size:
        st.status, st.detail = "corrupt", f"size mismatch: {', '.join(wrong_size[:3])}"
    elif bad_hash:
        st.status, st.detail = "corrupt", f"sha256 mismatch: {', '.join(bad_hash[:3])}"
    elif no_hash:
        st.status, st.detail = "unverified_hash", f"{len(no_hash)} file(s) have no pinned sha256"
    else:
        st.detail = f"{len(files)} files" + (" · sha256 verified" if full else " · sizes checked")
        st.full_verified = full
    st.problems += [f"missing: {m}" for m in missing]
    return st


def verify_all(config_dir: Path, models_root: Path, full: bool = False,
               cache: HashCache | None = None) -> dict[str, ModelStatus]:
    lock = load_lock(config_dir)
    return {k: verify_model(k, v, models_root, full, cache) for k, v in lock["models"].items()}
