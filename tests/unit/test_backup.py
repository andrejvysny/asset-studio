"""Backup + restore verification (Phase 0 / IM11 groundwork). CPU only."""
from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

from assetstudio_storage.backup import create_backup, verify_backup

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "projects" / "60ff832"


def test_backup_roundtrip_and_tamper_detection(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    shutil.copytree(FIXTURE / "project", root)
    path = create_backup(root, "prj_test", tmp_path / "out", FIXTURE / "journal.sqlite")
    rep = verify_backup(path)
    assert rep.ok, rep.problems
    assert rep.blobs_checked > 5 and rep.files > 20
    # tamper: rewrite one blob member with different bytes of the same size
    bad = tmp_path / "bad.tar"
    with tarfile.open(path) as src, tarfile.open(bad, "w", format=tarfile.PAX_FORMAT) as dst:
        done = False
        for m in src.getmembers():
            data = src.extractfile(m).read()  # type: ignore[union-attr]
            if not done and m.name.startswith("project/blobs/sha256/"):
                data = bytes([data[0] ^ 0xFF]) + data[1:]
                done = True
            import io

            dst.addfile(m, io.BytesIO(data))
    rep = verify_backup(bad)
    assert not rep.ok and any("mismatch" in p for p in rep.problems)
