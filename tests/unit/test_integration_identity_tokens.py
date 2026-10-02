"""Integration server identity, client token store and bind policy."""
from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

import pytest
from assetstudio_core.ids import new_id
from assetstudio_server.integration_api import identity
from assetstudio_server.integration_api.app import bind_problem
from assetstudio_server.integration_api.tokens import IntegrationTokenStore
from assetstudio_server.mcp_api.auth import TokenStore

from tests.conftest import make_settings

PRJ = new_id("prj")


def test_identity_persists_and_is_private(tmp_path: Path) -> None:
    path = tmp_path / "integration" / "server.json"
    first = identity.load_or_create(path)
    assert str(uuid.UUID(first.server_id)) == first.server_id
    assert identity.load_or_create(path) == first
    assert path.stat().st_mode & 0o077 == 0


def test_corrupt_identity_raises_instead_of_regenerating(tmp_path: Path) -> None:
    path = tmp_path / "server.json"
    path.write_text("{not json")
    with pytest.raises(identity.IdentityError):
        identity.load_or_create(path)
    assert path.read_text() == "{not json"
    path.write_text(json.dumps({"server_id": "NOT-A-UUID", "created_at": "x"}))
    with pytest.raises(identity.IdentityError):
        identity.load_or_create(path)


def test_adopt_requires_force_and_valid_uuid(tmp_path: Path) -> None:
    path = tmp_path / "server.json"
    existing = identity.load_or_create(path)
    other = str(uuid.uuid4())
    with pytest.raises(identity.IdentityError):
        identity.adopt(path, other, force=False)
    with pytest.raises(identity.IdentityError):
        identity.adopt(path, "nope", force=True)
    with pytest.raises(identity.IdentityError):
        identity.adopt(path, other.upper(), force=True)
    assert identity.load_or_create(path) == existing
    assert identity.adopt(path, other, force=True).server_id == other
    assert identity.load_or_create(path).server_id == other


def test_tokens_hashed_private_and_listing_hides_hash(tmp_path: Path) -> None:
    store = IntegrationTokenStore(tmp_path / "integration" / "tokens.json")
    token = store.create("godot", ["assets:read"], [PRJ, PRJ])
    assert token.startswith("asi_")
    raw = store.path.read_text()
    assert token not in raw and "sha256" in raw
    assert store.path.stat().st_mode & 0o077 == 0
    listed = store.list()
    assert listed[0]["library_ids"] == [PRJ] and "sha256" not in listed[0]
    found = store.verify(token)
    assert found is not None and found.scopes == ["assets:read"]
    assert store.verify(token + "x") is None


def test_token_validation(tmp_path: Path) -> None:
    store = IntegrationTokenStore(tmp_path / "t.json")
    for name, scopes, libs in (("Bad Name", ["assets:read"], [PRJ]), ("ok", [], [PRJ]), ("ok", ["admin"], [PRJ]),
                               ("ok", ["assets:read"], []), ("ok", ["assets:read"], ["prj_bad"]),
                               ("ok", ["assets:read"], [new_id("ast")])):
        with pytest.raises(ValueError):
            store.create(name, scopes, libs)
    assert store.list() == []


def test_revocation_is_seen_by_another_store_instance_and_name_is_reusable(tmp_path: Path) -> None:
    server = IntegrationTokenStore(tmp_path / "tokens.json")
    token = server.create("godot", ["assets:read"], [PRJ])
    assert server.verify(token) is not None
    cli = IntegrationTokenStore(server.path)  # the CLI is another process with its own store
    assert cli.revoke("godot") and not cli.revoke("godot")
    assert server.verify(token) is None
    again = server.create("godot", ["assets:publish"], [PRJ])
    with pytest.raises(ValueError):
        cli.create("godot", ["assets:read"], [PRJ])  # active duplicate
    assert server.verify(again) is not None and server.verify(token) is None
    entries = cli.list()
    assert [e["revoked_at"] is not None for e in entries] == [True, False]


def test_publish_scope_does_not_imply_read(tmp_path: Path) -> None:
    store = IntegrationTokenStore(tmp_path / "t.json")
    found = store.verify(store.create("pub", ["assets:publish"], [PRJ]))
    assert found is not None and found.scopes == ["assets:publish"]


def test_mcp_token_is_not_an_integration_token(tmp_path: Path) -> None:
    mcp = TokenStore(tmp_path / "mcp.json").create("agent", "full")
    assert mcp.startswith("ast_")
    assert IntegrationTokenStore(tmp_path / "t.json").verify(mcp) is None


def test_bind_problem_matrix(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    for host in ("127.0.0.1", "::1", "localhost"):
        s.integration_host = host
        assert bind_problem(s) is None
    s.integration_host = "0.0.0.0"
    assert "refusing" in (bind_problem(s) or "")
    s.integration_tls_cert = "/c.pem"
    assert bind_problem(s) is not None  # key missing
    s.integration_tls_key = "/k.pem"
    assert bind_problem(s) is None
    s.integration_tls_cert = s.integration_tls_key = ""
    s.integration_allow_insecure_lan = True
    assert bind_problem(s) is None
    s.integration_allow_insecure_lan = False
    s.integration_container_bind = True
    assert bind_problem(s) is None


def _token_args(name: str, library: str, token_file: Path) -> argparse.Namespace:
    return argparse.Namespace(action="create", name=name, library=[library], scope=None, token_file=str(token_file))


def test_cli_token_file_is_private_and_not_printed(tmp_path: Path, capsys) -> None:
    from assetstudio_server.cli import _integration_token
    from assetstudio_server.registry import Registry

    s = make_settings(tmp_path)
    s.ensure()
    lib = Registry(s).create("Lib", tmp_path / "projects" / "lib").id
    store = IntegrationTokenStore(tmp_path / "tokens.json")
    out = tmp_path / "token.txt"
    assert _integration_token(s, _token_args("godot", lib, out), store) == 0
    token = out.read_text().strip()
    assert out.stat().st_mode & 0o777 == 0o600
    assert token not in capsys.readouterr().out and store.verify(token) is not None
    assert _integration_token(s, _token_args("other", lib, out), store) == 2  # never overwrites
    assert out.read_text().strip() == token and [t["name"] for t in store.list()] == ["godot"]
