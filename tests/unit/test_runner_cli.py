"""`assetstudio runners ...` against a temporary instance dir (auth DB opened directly, no server)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from assetstudio_client import keys
from assetstudio_server import cli
from assetstudio_server.authstore import AuthStore


@pytest.fixture
def instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("STUDIO_INSTANCE_DIR", str(tmp_path / "inst"))
    return tmp_path / "inst"


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(["runners", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_group_token_list_revoke(instance: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "group-create", "--name", "lab", "--projects", "prj_a,prj_b", "--labels", "gpu,fast",
                       "--ephemeral")
    group = json.loads(out)
    assert code == 0 and group["projects"] == ["prj_a", "prj_b"] and group["operations"] == "*"
    assert group["labels"] == ["gpu", "fast"] and group["ephemeral"] is True and group["created_by"] == "cli"
    assert run(capsys, "group-create", "--name", "lab")[0] == 1  # duplicate name

    for ref in ("lab", group["id"]):  # by name or id; stdout is the token only
        code, out, _ = run(capsys, "token", "--group", ref, "--ttl", "300")
        assert code == 0 and out.strip() and " " not in out.strip() and "\n" not in out.strip()
    assert run(capsys, "token", "--group", "missing")[0] == 1
    assert run(capsys, "token", "--group", "lab", "--ttl", "99999")[0] == 1

    token = run(capsys, "token", "--group", "lab")[1].strip()
    store = AuthStore(instance / "auth.sqlite")
    priv = keys.generate_private_key()
    runner = store.register_runner(token, keys.public_key_b64(priv), "box", {"os": "l", "arch": "a", "hostname": "h"})
    store.close()

    code, out, _ = run(capsys, "list")
    assert code == 0 and runner["id"] in out and "box" in out and "active" in out
    assert run(capsys, "revoke", runner["id"])[0] == 0
    assert "revoked" in run(capsys, "list")[1]
    assert run(capsys, "revoke", "rnr_nope")[0] == 1
