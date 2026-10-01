"""AuthStore: 0600 file, hashed secrets, registration, nonces, access tokens, audit."""
from __future__ import annotations

import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from assetstudio_server.authstore import AuthStore, AuthStoreTooNew, RegistrationRefused, Unauthorized


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, s: int) -> None:
        self.t += timedelta(seconds=s)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> AuthStore:
    s = AuthStore(tmp_path / "inst" / "auth.sqlite", now=clock)
    yield s
    s.close()


def _group(s: AuthStore, name: str = "g") -> str:
    return s.create_group(name, ["prj_1"], "*", ["gpu"], False, "admin")["id"]


def _register(s: AuthStore, key: str = "pk1", ttl: int = 600) -> tuple[str, dict]:
    tok = s.create_registration_token(_group(s, f"g-{key}"), ttl, "admin")
    return tok, s.register_runner(tok, key, "box", {"os": "linux"})


def test_file_mode_and_too_new(tmp_path: Path) -> None:
    path = tmp_path / "auth.sqlite"
    AuthStore(path).close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    c = sqlite3.connect(path)
    c.execute("PRAGMA user_version=99")
    c.close()
    with pytest.raises(AuthStoreTooNew):
        AuthStore(path)


def test_sidecars_private_while_open(tmp_path: Path) -> None:
    path = tmp_path / "auth.sqlite"
    files = [path, path.with_name("auth.sqlite-wal"), path.with_name("auth.sqlite-shm")]
    old = os.umask(0o022)
    try:
        for n in range(2):  # fresh, then reopened
            s = AuthStore(path)
            s.add_studio_key(f"k{n}", b"private", "pub", "current")
            assert [stat.S_IMODE(f.stat().st_mode) for f in files] == [0o600] * 3
            s.close()
    finally:
        os.umask(old)


def test_groups(store: AuthStore) -> None:
    g = store.create_group("a", "*", ["image.generate"], ["x"], True, "admin")
    assert g["projects"] == "*" and g["ephemeral"] is True and store.get_group(g["id"]) == g
    with pytest.raises(ValueError):
        store.create_group("a", [], [], [], False, "admin")
    assert len(store.groups()) == 1


def test_secrets_never_stored_plaintext(store: AuthStore, tmp_path: Path) -> None:
    tok, r = _register(store)
    nonce, _ = store.issue_nonce(r["id"], "aud")
    access, _ = store.issue_access_token(r["id"])
    for table in ("registration_tokens", "access_tokens"):
        for row in store._db.execute(f"SELECT * FROM {table}"):
            assert tok not in tuple(row) and access not in tuple(row)
    # the nonce is the one deliberate plaintext value
    assert store._db.execute("SELECT nonce FROM nonces").fetchone()[0] == nonce


def test_registration_paths(store: AuthStore, clock: Clock) -> None:
    tok, r = _register(store, "pk1")
    assert r["state"] == "active" and r["id"].startswith("rnr_") and r["platform"] == {"os": "linux"}
    assert store.register_runner(tok, "pk1", "box", {})["id"] == r["id"]  # idempotent retry
    with pytest.raises(RegistrationRefused) as e:
        store.register_runner(tok, "pk2", "box", {})
    assert e.value.reason == "already_used"
    with pytest.raises(RegistrationRefused) as e:
        store.register_runner("nope", "pk3", "box", {})
    assert e.value.reason == "unknown_token"
    gid = _group(store, "other")
    t2 = store.create_registration_token(gid, 60, "admin")
    with pytest.raises(RegistrationRefused) as e:
        store.register_runner(t2, "pk1", "box", {})
    assert e.value.reason == "key_in_use"
    t3 = store.create_registration_token(gid, 60, "admin")
    clock.advance(61)
    with pytest.raises(RegistrationRefused) as e:
        store.register_runner(t3, "pk4", "box", {})
    assert e.value.reason == "expired"
    with pytest.raises(ValueError):
        store.create_registration_token(gid, 3601, "admin")
    with pytest.raises(KeyError):
        store.create_registration_token("rgp_missing", 60, "admin")
    events = [a["event"] for a in store.audit_log()]
    assert events.count("register") == 2 and events.count("register_refused") == 4
    assert store.audit_log(runner_id=r["id"])[0]["event"] == "register"


def test_runner_misc(store: AuthStore) -> None:
    _, r = _register(store)
    store.set_push_url(r["id"], "http://x")
    store.touch_runner(r["id"])
    got = store.get_runner(r["id"])
    assert got and got["push_url"] == "http://x" and got["last_seen_at"]
    assert len(store.runners(r["group_id"])) == 1 and store.runners("rgp_none") == []


def test_nonce_rules(store: AuthStore, clock: Clock) -> None:
    _, r = _register(store)
    rid = r["id"]
    n, exp = store.issue_nonce(rid, "aud")
    assert not store.consume_nonce(n, rid, "other") and not store.consume_nonce(n, "rnr_x", "aud")
    assert store.consume_nonce(n, rid, "aud") and not store.consume_nonce(n, rid, "aud")
    n2, _ = store.issue_nonce(rid, "aud", ttl_s=10)
    clock.advance(11)
    assert not store.consume_nonce(n2, rid, "aud")
    for _ in range(3):
        store.issue_nonce(rid, "aud", max_outstanding=3)
    with pytest.raises(Unauthorized) as e:
        store.issue_nonce(rid, "aud", max_outstanding=3)
    assert e.value.reason == "too_many_nonces"
    with pytest.raises(Unauthorized) as e:
        store.issue_nonce("rnr_unknown", "aud")
    assert e.value.reason == "unknown_runner"


def test_access_tokens_and_revocation(store: AuthStore, clock: Clock) -> None:
    _, r = _register(store)
    tok, _ = store.issue_access_token(r["id"], ttl_s=30)
    assert store.resolve_access_token(tok)["id"] == r["id"]  # type: ignore[index]
    assert store.resolve_access_token("bogus") is None
    clock.advance(31)
    assert store.resolve_access_token(tok) is None
    tok2, _ = store.issue_access_token(r["id"])
    n, _ = store.issue_nonce(r["id"], "aud")
    assert store.revoke_runner(r["id"], "admin") and not store.revoke_runner(r["id"], "admin")
    assert store.resolve_access_token(tok2) is None and not store.consume_nonce(n, r["id"], "aud")
    assert store.get_runner(r["id"])["state"] == "revoked"  # type: ignore[index]
    for call in (lambda: store.issue_access_token(r["id"]), lambda: store.issue_nonce(r["id"], "aud")):
        with pytest.raises(Unauthorized) as e:
            call()
        assert e.value.reason == "revoked"
    assert "revoke" in [a["event"] for a in store.audit_log(runner_id=r["id"])]


def test_studio_keys(store: AuthStore) -> None:
    store.add_studio_key("k1", b"secret", "pub1", "current")
    store.add_studio_key("k2", b"secret2", "pub2", "next")
    store.add_studio_key("k0", b"old", "pub0", "retired")
    assert [k["key_id"] for k in store.studio_keys()] == ["k1", "k2"]
    assert all("private_key" not in k for k in store.studio_keys())
    assert store.studio_keys(include_private=True)[0]["private_key"] == b"secret"
    assert store.set_studio_key_status("k2", "current") and not store.set_studio_key_status("zz", "current")
    store.audit("x", "actor", detail={"a": 1})
    assert store.audit_log(limit=1)[0]["detail"] == {"a": 1}
