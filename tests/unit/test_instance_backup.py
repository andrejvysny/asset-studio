"""Instance backup: identity + token stores + journal snapshot, verify detects tampering, no plaintext tokens."""
from __future__ import annotations

import io
import json
import sqlite3
import tarfile
import uuid
from pathlib import Path

from assetstudio_core.ids import new_id
from assetstudio_server.authstore import AuthStore
from assetstudio_server.cli import main
from assetstudio_server.instance_backup import create_instance_backup, verify_instance_backup
from assetstudio_server.integration_api.tokens import IntegrationTokenStore
from assetstudio_server.mcp_api.auth import TokenStore

from tests.conftest import make_settings


def _instance(tmp_path: Path):  # noqa: ANN202
    s = make_settings(tmp_path)
    s.ensure()
    sid = str(uuid.uuid4())
    s.integration_dir.mkdir(parents=True, exist_ok=True)
    (s.integration_dir / "server.json").write_text(json.dumps({"server_id": sid, "created_at": "2026-01-01T00:00:00"}))
    plain = IntegrationTokenStore(s.integration_dir / "tokens.json").create("godot", ["assets:read"], [new_id("prj")])
    mcp_plain = TokenStore(s.instance_dir / "mcp_tokens.json").create("agent", "full")
    AuthStore(s.instance_dir / "auth.sqlite").close()  # node mode: runner credentials + signing keys
    con = sqlite3.connect(s.instance_dir / "journal" / "operations.sqlite")
    con.execute("create table t(x)")
    con.execute("insert into t values (1)")
    con.commit()
    con.close()
    return s, sid, [plain, mcp_plain]


def _rewrite(src: Path, dst: Path, tamper: str) -> None:
    with tarfile.open(src) as t, tarfile.open(dst, "w:gz") as out:
        for m in t.getmembers():
            data = t.extractfile(m).read()  # type: ignore[union-attr]
            if m.name == tamper:
                data += b" "
            m.size = len(data)
            out.addfile(m, io.BytesIO(data))


def test_round_trip_contains_state_and_no_plaintext(tmp_path: Path) -> None:
    s, sid, plains = _instance(tmp_path)
    path = create_instance_backup(s, tmp_path / "out")
    rep = verify_instance_backup(path)
    assert rep.ok, rep.problems
    assert rep.server_id == sid
    assert set(rep.members) == {"integration/server.json", "integration/tokens.json", "mcp_tokens.json",
                                "journal/operations.sqlite", "auth.sqlite"}
    with tarfile.open(path) as t:
        blob = b"".join(t.extractfile(m).read() for m in t.getmembers())  # type: ignore[union-attr]
    assert all(p.encode() not in blob for p in plains)
    assert path.name.startswith("instance-") and path.name.endswith(".tar.gz")


def test_tampered_member_fails_verify(tmp_path: Path) -> None:
    s, _, _ = _instance(tmp_path)
    path = create_instance_backup(s, tmp_path / "out")
    bad = tmp_path / "bad.tar.gz"
    _rewrite(path, bad, "integration/tokens.json")
    rep = verify_instance_backup(bad)
    assert not rep.ok and any("mismatch" in p for p in rep.problems)


def test_cli_backup_and_verify(tmp_path: Path, monkeypatch, capsys) -> None:
    s, _, _ = _instance(tmp_path)
    monkeypatch.setenv("STUDIO_INSTANCE_DIR", str(s.instance_dir))
    assert main(["instance", "backup", "--out", str(tmp_path / "o")]) == 0
    archive = json.loads(capsys.readouterr().out)["backup"]
    assert main(["instance", "restore-verify", archive]) == 0


def test_master_backup_without_auth_store_still_verifies() -> None:
    """Backups made before auth.sqlite existed (master@90071ad) stay valid restore points."""
    path = Path(__file__).resolve().parents[1] / "fixtures" / "journal_v3_master" / "instance-backup-master.tar.gz"
    rep = verify_instance_backup(path)
    assert rep.ok, rep.problems
    assert "auth.sqlite" not in rep.members and "journal/operations.sqlite" in rep.members
