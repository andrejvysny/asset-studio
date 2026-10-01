"""`assetstudio integration ...` against a temp instance dir (works offline, touches files directly)."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from assetstudio_core.ids import new_id
from assetstudio_server import cli
from assetstudio_server.registry import Registry
from assetstudio_server.settings import Settings


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("STUDIO_INSTANCE_DIR", str(tmp_path / "instance"))
    monkeypatch.setenv("STUDIO_PROJECT_ROOTS", str(tmp_path / "projects"))
    s = Settings()
    s.ensure()
    return s


def _project(s: Settings) -> str:
    return Registry(s).create("Demo", s.project_roots[0] / "demo", starter_qa=False).id


def test_token_create_list_revoke(env: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    lib = _project(env)
    assert cli.main(["integration", "token", "create", "godot", "--library", lib]) == 0
    out = capsys.readouterr().out
    token = next(line for line in out.splitlines() if line.startswith("asi_"))
    assert out.count(token) == 1 and "/api/integration/v1" in out
    assert cli.main(["integration", "token", "list"]) == 0
    listing = capsys.readouterr().out
    assert token not in listing and "sha256" not in listing
    entry = json.loads(listing)[0]
    assert entry["name"] == "godot" and entry["scopes"] == ["assets:read"] and entry["library_ids"] == [lib]
    assert cli.main(["integration", "token", "revoke", "godot"]) == 0
    assert cli.main(["integration", "token", "revoke", "godot"]) == 1
    assert cli.main(["integration", "token", "revoke", "never"]) == 1


def test_token_create_rejects_unknown_library_and_bad_scope(env: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["integration", "token", "create", "godot", "--library", new_id("prj")]) == 2
    assert "unknown library" in capsys.readouterr().err
    lib = _project(env)
    with pytest.raises(SystemExit):
        cli.main(["integration", "token", "create", "godot", "--library", lib, "--scope", "admin"])
    assert cli.main(["integration", "token", "create", "godot", "--library", lib, "--scope", "assets:read",
                     "--scope", "assets:publish"]) == 0
    capsys.readouterr()
    cli.main(["integration", "token", "list"])
    assert json.loads(capsys.readouterr().out)[0]["scopes"] == ["assets:publish", "assets:read"]


def test_identity_show_and_adopt(env: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["integration", "identity", "show"]) == 0
    first = json.loads(capsys.readouterr().out)["server_id"]
    cli.main(["integration", "identity", "show"])
    assert json.loads(capsys.readouterr().out)["server_id"] == first
    other = str(uuid.uuid4())
    assert cli.main(["integration", "identity", "adopt", other]) == 2
    assert "never for forks" in capsys.readouterr().err
    cli.main(["integration", "identity", "show"])
    assert json.loads(capsys.readouterr().out)["server_id"] == first
    assert cli.main(["integration", "identity", "adopt", other, "--i-understand-fork"]) == 0
    capsys.readouterr()
    cli.main(["integration", "identity", "show"])
    assert json.loads(capsys.readouterr().out)["server_id"] == other
    assert cli.main(["integration", "identity", "adopt", "bad", "--i-understand-fork"]) == 2
