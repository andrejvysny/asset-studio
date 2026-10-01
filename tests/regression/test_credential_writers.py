"""H01: concurrent credential writers (separate store instances / processes) never undo each other's revocation."""
from __future__ import annotations

import multiprocessing
import threading
from pathlib import Path

from assetstudio_core.ids import is_id, new_id
from assetstudio_server.integration_api.tokens import IntegrationTokenStore
from assetstudio_server.mcp_api.auth import TokenStore

PRJ = new_id("prj")


def test_stale_writer_cannot_resurrect_a_revoked_token(tmp_path: Path) -> None:
    """The reviewed interleaving: A loads, B revokes beta and saves, A saves. With the writer lock B waits for A."""
    path = tmp_path / "tokens.json"
    seed = IntegrationTokenStore(path)
    alpha = seed.create("alpha", ["assets:read"], [PRJ])
    beta = seed.create("beta", ["assets:read"], [PRJ])
    a, b = IntegrationTokenStore(path), IntegrationTokenStore(path)
    loaded, resume = threading.Event(), threading.Event()
    original = a._load

    def paused_load(force: bool = False) -> None:
        original(force)
        loaded.set()
        assert resume.wait(5)

    a._load = paused_load  # type: ignore[method-assign]
    writer = threading.Thread(target=a.revoke, args=("alpha",))
    writer.start()
    assert loaded.wait(5)
    other = threading.Thread(target=b.revoke, args=("beta",))
    other.start()
    other.join(0.3)
    assert other.is_alive(), "second writer must wait for the first writer's transaction"
    resume.set()
    writer.join(5)
    other.join(5)
    reader = IntegrationTokenStore(path)
    assert reader.verify(alpha) is None and reader.verify(beta) is None


def _revoke(path: str, name: str, barrier: multiprocessing.synchronize.Barrier) -> None:
    barrier.wait(10)
    assert IntegrationTokenStore(Path(path)).revoke(name)


def test_processes_revoking_different_tokens_concurrently(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    seed = IntegrationTokenStore(path)
    names = [f"t{i}" for i in range(6)]
    secrets = [seed.create(n, ["assets:read"], [PRJ]) for n in names]
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(len(names))
    procs = [ctx.Process(target=_revoke, args=(str(path), n, barrier)) for n in names]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    assert all(p.exitcode == 0 for p in procs)
    reader = IntegrationTokenStore(path)  # a fresh process view, as after a restart
    assert all(reader.verify(s) is None for s in secrets)
    assert all(e["revoked_at"] is not None for e in reader.list())


def test_credential_identity_is_immutable_and_not_the_name(tmp_path: Path) -> None:
    store = IntegrationTokenStore(tmp_path / "tokens.json")
    first = store.create("godot", ["assets:publish"], [PRJ])
    old = store.verify(first)
    assert old is not None and is_id(old.credential_id, "icr")
    assert store.revoke("godot")
    second = store.create("godot", ["assets:publish"], [PRJ])
    new = store.verify(second)
    assert new is not None and new.credential_id != old.credential_id
    assert IntegrationTokenStore(store.path).verify(second).credential_id == new.credential_id  # type: ignore[union-attr]


def test_legacy_entries_get_a_stable_derived_identity(tmp_path: Path) -> None:
    store = IntegrationTokenStore(tmp_path / "tokens.json")
    token = store.create("godot", ["assets:read"], [PRJ])
    raw = store.path.read_text().replace('"credential_id"', '"_dropped"')
    import json

    doc = json.loads(raw)
    for t in doc["tokens"]:
        t.pop("_dropped")
    store.path.write_text(json.dumps(doc))
    ids = {IntegrationTokenStore(store.path).verify(token).credential_id for _ in range(2)}  # type: ignore[union-attr]
    assert len(ids) == 1 and is_id(ids.pop(), "icr")


def test_mcp_writers_are_serialized_too(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    seed = TokenStore(path)
    tokens = {n: seed.create(n, "read") for n in ("a", "b", "c", "d")}
    threads = [threading.Thread(target=TokenStore(path).revoke, args=(n,)) for n in tokens]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    reader = TokenStore(path)
    assert all(reader.verify(t) is None for t in tokens.values()) and reader.list() == []
