#!/usr/bin/env python3
"""Isolated, dependency-free review probes for AssetStudio commit 11f5b18.

These are NOT the repository test suite or an end-to-end application test.
The token store, EventBus, and replay guard below are transcribed source excerpts.
External dependencies are replaced by small test doubles. The lock probe models
exactly the nested per-library lock acquisition, with a timeout to avoid hanging.
The capability probe evaluates the source-delivery construction expression.
Run: python review_probes.py
No network, production files, GPU, or persistent real credentials are used.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REV = "11f5b18b5f58f11281cc5837d155ede91bf9b5f6"
ROOT = f"https://github.com/andrejvysny/asset-studio/blob/{REV}/"
SOURCES = {
    "tokens": ROOT + "services/studio/assetstudio_server/integration_api/tokens.py",
    "private_files": ROOT + "services/studio/assetstudio_server/secure_files.py",
    "events": ROOT + "services/studio/assetstudio_server/events.py",
    "changes": ROOT + "services/studio/assetstudio_server/integration_api/routes_changes.py",
    "publication": ROOT + "services/studio/assetstudio_server/services/source_publications.py",
    "deliveries": ROOT + "services/studio/assetstudio_server/services/deliveries.py",
}

# Source excerpt: secure_files.py (unchanged function bodies).
def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
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


# Source excerpts: ClientToken and the exercised IntegrationTokenStore methods.
TOKEN_PREFIX = "asi_"


@dataclass
class ClientToken:
    name: str
    sha256: str
    scopes: list[str]
    library_ids: list[str]
    created_at: str
    revoked_at: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class IntegrationTokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._tokens: list[ClientToken] = []
        self._sig: tuple[int, int, int] | None = None

    def _load(self) -> None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._tokens, self._sig = [], None
            return
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        if sig == self._sig:
            return
        raw = json.loads(self.path.read_text())
        self._tokens = [ClientToken(**t) for t in raw.get("tokens", [])]
        self._sig = sig

    def _save(self) -> None:
        write_private(self.path, {"tokens": [asdict(t) for t in self._tokens]})
        st = self.path.stat()
        self._sig = (st.st_mtime_ns, st.st_size, st.st_ino)

    def revoke(self, name: str) -> bool:
        with self._lock:
            self._load()
            live = next((t for t in self._tokens if t.name == name and t.revoked_at is None), None)
            if live is None:
                return False
            live.revoked_at = _now()
            self._save()
            return True

    def verify(self, token: str) -> ClientToken | None:
        if not token.startswith(TOKEN_PREFIX):
            return None
        digest = _digest(token)
        with self._lock:
            self._load()
            found: ClientToken | None = None
            for t in self._tokens:
                if t.revoked_at is None and hmac.compare_digest(t.sha256, digest):
                    found = t
            return found


def probe_token_revocation() -> dict[str, Any]:
    # Two store instances have independent locks, as two CLI processes would.
    # Pause one after read, revoke another token, then let the stale writer save.
    with tempfile.TemporaryDirectory(prefix="assetstudio-review-") as tmp:
        path = Path(tmp) / "tokens.json"
        alpha = TOKEN_PREFIX + secrets.token_urlsafe(32)
        beta = TOKEN_PREFIX + secrets.token_urlsafe(32)
        write_private(path, {"tokens": [
            asdict(ClientToken("alpha", _digest(alpha), ["assets:read"], ["prj_0000000000000000"], _now())),
            asdict(ClientToken("beta", _digest(beta), ["assets:read"], ["prj_0000000000000000"], _now())),
        ]})
        a, b = IntegrationTokenStore(path), IntegrationTokenStore(path)
        loaded, resume = threading.Event(), threading.Event()
        original_load = a._load
        errors: list[BaseException] = []

        def paused_load() -> None:
            original_load()
            loaded.set()
            if not resume.wait(5):
                raise TimeoutError("test synchronization failed")

        a._load = paused_load

        def writer_a() -> None:
            try:
                assert a.revoke("alpha")
            except BaseException as e:
                errors.append(e)

        thread = threading.Thread(target=writer_a)
        thread.start()
        assert loaded.wait(5)
        try:
            assert b.revoke("beta")
            assert IntegrationTokenStore(path).verify(beta) is None
        finally:
            resume.set()
            thread.join(5)
        assert not thread.is_alive() and not errors, errors
        reader = IntegrationTokenStore(path)
        resurrected = reader.verify(beta) is not None
        assert resurrected, "the reviewed race did not reproduce"
        assert reader.verify(alpha) is None
        return {"finding": "H01", "probe": "independent token-store writers", "reproduced": True,
                "observation": "beta was revoked, then became valid after alpha's stale writer saved",
                "scope": "source excerpt; temporary files; controlled interleaving; not a live server test"}


# Source excerpt: events.py. Only the now_iso import is replaced with this stub.
def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class EventBus:
    def __init__(self, maxlen: int = 5000) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._seq = 0
        self._cond = threading.Condition()
        self.epoch = f"{time.time_ns():x}-{secrets.token_hex(4)}"

    def publish(self, type_: str, **fields: Any) -> None:
        with self._cond:
            self._seq += 1
            self._events.append({"seq": self._seq, "time": now_iso(), "type": type_, **fields})
            self._cond.notify_all()

    def since(self, cursor: int, timeout: float = 15.0) -> tuple[list[dict[str, Any]], bool]:
        with self._cond:
            if not any(e["seq"] > cursor for e in self._events) and cursor >= self._seq:
                self._cond.wait(timeout)
            oldest = self._events[0]["seq"] if self._events else self._seq + 1
            expired = cursor + 1 < oldest and cursor < self._seq
            return [e for e in self._events if e["seq"] > cursor], expired

    @property
    def seq(self) -> int:
        return self._seq


# Source excerpts: routes_changes.py.
MAX_EVENTS = 500
ASSET_TYPES = {"published": "asset_current_changed", "current": "asset_current_changed",
               "metadata": "asset_metadata_changed", "delivery_ready": "delivery_ready"}


def project_event(event: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any] | None:
    library_id = event.get("project_id")
    if event.get("type") != "library" or library_id not in allowed:
        return None
    asset_id = event.get("asset_id")
    kind = ASSET_TYPES.get(event.get("change", ""))
    if kind is None or not asset_id:
        return {"type": "library_changed", "library_id": library_id}
    return {"type": kind, "library_id": library_id, "asset_id": asset_id}


def _collect(bus: EventBus, pos: int, allowed: frozenset[str]) -> tuple[int, list[dict[str, Any]]]:
    batch, _ = bus.since(pos, timeout=0)
    out: list[dict[str, Any]] = []
    for e in batch:
        if len(out) >= MAX_EVENTS:
            break
        pos = e["seq"]
        shown = project_event(e, allowed)
        if shown is not None:
            out.append(shown)
    return pos, out


def probe_change_feed() -> dict[str, Any]:
    bus = EventBus(maxlen=2)
    pos = 0
    # This is the initial route check, before its async poll/sleep interval.
    assert not bus.since(pos, timeout=0)[1]
    for i in range(4):
        bus.publish("library", project_id="L", asset_id=f"asset-{i + 1}", change="published")
    assert bus.since(pos, timeout=0)[1] is True
    new_pos, shown = _collect(bus, pos, frozenset({"L"}))
    assert new_pos == 4 and [e["asset_id"] for e in shown] == ["asset-3", "asset-4"]
    return {"finding": "H08", "probe": "change-feed overflow after initial validation", "reproduced": True,
            "observation": "cursor advances 0 -> 4; events 1 and 2 are missing; _collect drops expired=True",
            "scope": "source-excerpt EventBus and _collect; reduced ring capacity; not an HTTP test"}


# Source excerpt: source_publications._check_same_request. The store/domain names
# below are test doubles; the conditional and lookup structure are unchanged.
class AssetVersion:
    pass


class IntegrationError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


def version_key(asset_id: str, version_id: str) -> tuple[str, str]:
    return asset_id, version_id


def _check_same_request(ctx: Any, done: dict[str, Any], req: Any) -> None:
    version = ctx.store.get(version_key(done["asset_id"], done["version_id"]), AssetVersion)[0]
    mine = version.sources.get("integration", {})
    if mine.get("preview_id") != req.preview_id or mine.get("package_sha256") != req.package_sha256:
        raise IntegrationError(409, "idempotency_conflict", "idempotency key reused with a different request")


def probe_replay_guard() -> dict[str, Any]:
    version = SimpleNamespace(sources={"integration": {"preview_id": "ipv_test", "package_sha256": "a" * 64}})
    ctx = SimpleNamespace(store=SimpleNamespace(get=lambda *_: (version, None)))
    done = {"asset_id": "original-asset", "version_id": "original-version"}
    req = SimpleNamespace(preview_id="ipv_test", package_sha256="a" * 64,
                          portable_sha256="b" * 64, descriptor_draft_sha256="c" * 64,
                          target_asset_id="different-asset", expected_current_version="different-version",
                          name="different name", licence="different declaration")
    _check_same_request(ctx, done, req)
    req.preview_id = "different-preview"
    try:
        _check_same_request(ctx, done, req)
    except IntegrationError as e:
        assert e.code == "idempotency_conflict"
    else:
        raise AssertionError("control case should reject")
    return {"finding": "H02", "probe": "canonical-receipt replay guard", "reproduced": True,
            "observation": "changed portable/draft hash, target, expected pointer, name and licence are not compared",
            "scope": "exact guard with a fake store; not the complete commit route"}


def probe_source_capabilities() -> dict[str, Any]:
    # Exact expression used by services/deliveries.py:_published for source.
    parsed = SimpleNamespace(collision=None)
    caps = ["godot_text_scene_v1"] + (["static_collision"] if parsed.collision else [])
    detected = {"godot_text_scene_v1", "csg_static", "shader_source"}
    omitted = sorted(detected - set(caps))
    assert omitted == ["csg_static", "shader_source"]
    return {"finding": "H03", "probe": "source-delivery capability expression", "reproduced": True,
            "observation": {"detected": sorted(detected), "emitted": caps, "omitted": omitted},
            "scope": "source expression, synthetic detected metadata; not actual fixture publication"}


def probe_library_lock_order() -> dict[str, Any]:
    # Model ensure_version(A1) -> _published -> _add_dependency(B0) -> ensure_version(B0),
    # alongside ensure_version(B1) -> _published -> _add_dependency(A0) -> ensure_version(A0).
    # The version graph is acyclic: only new versions depend on independent old versions.
    locks = {"A": threading.RLock(), "B": threading.RLock()}
    both_outer = threading.Barrier(2)
    both_nested_attempted = threading.Barrier(2)
    blocked: list[str] = []
    errors: list[BaseException] = []

    def prepare(outer: str, dependency: str) -> None:
        try:
            with locks[outer]:
                both_outer.wait(5)
                acquired = locks[dependency].acquire(timeout=0.25)
                if not acquired:
                    blocked.append(f"{outer} waits for {dependency}")
                # Retain the outer locks until BOTH nested attempts have timed out.
                both_nested_attempted.wait(5)
                if acquired:
                    locks[dependency].release()
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=prepare, args=("A", "B")),
               threading.Thread(target=prepare, args=("B", "A"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(6)
    assert not errors and not any(t.is_alive() for t in threads), errors
    assert sorted(blocked) == ["A waits for B", "B waits for A"]
    return {"finding": "H04", "probe": "cross-library nested-lock interleaving", "reproduced": True,
            "observation": sorted(blocked),
            "scope": "lock topology microprobe, not full delivery service; timeouts prevent a hanging test"}


def main() -> None:
    results = [probe_token_revocation(), probe_replay_guard(), probe_source_capabilities(),
               probe_library_lock_order(), probe_change_feed()]
    print(json.dumps({"reviewed_commit": REV, "kind": "isolated source-excerpt/microprobes",
                      "not_repository_tests": True, "sources": SOURCES, "results": results}, indent=2))


if __name__ == "__main__":
    main()
