"""Project backup (uncompressed tar) with a hashed inventory, and CPU-only restore verification.

A backup holds the portable project root and a consistent copy of the instance journal (SQLite online-backup API):
operational command/task history is part of what must survive a migration, not a cache.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from assetstudio_core.canonical import now_iso

INVENTORY = "BACKUP.json"
FORMAT = "assetstudio.backup/1"
_CHUNK = 1 << 20
_SKIP = {"writer.lock"}


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _files(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            raise ValueError(f"refusing to back up a symlink: {p.relative_to(root)}")
        if p.is_file() and p.name not in _SKIP and not p.name.startswith(".tmp-"):
            out.append(p)
    return out


def create_backup(project_root: Path, project_id: str, out_dir: Path, journal: Path | None) -> Path:
    """Caller must hold the project's writer lock (no concurrent Studio writes)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_iso().replace(":", "").replace("-", "").split(".")[0]
    target = out_dir / f"{project_id}_{stamp}.tar"
    if target.exists():
        raise FileExistsError(target)
    entries: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(target, "x", format=tarfile.PAX_FORMAT) as tar:
        for p in _files(project_root):
            rel = f"project/{p.relative_to(project_root).as_posix()}"
            entries.append({"path": rel, "size": p.stat().st_size, "sha256": _sha(p)})
            tar.add(p, rel, recursive=False)
        if journal is not None and journal.is_file():
            copy = Path(tmp) / "journal.sqlite"
            src, dst = sqlite3.connect(journal), sqlite3.connect(copy)
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
            entries.append({"path": "journal.sqlite", "size": copy.stat().st_size, "sha256": _sha(copy)})
            tar.add(copy, "journal.sqlite", recursive=False)
        inv = json.dumps({"format": FORMAT, "project_id": project_id, "created_at": now_iso(),
                          "source_root": str(project_root), "files": entries}, indent=2).encode()
        inv_path = Path(tmp) / INVENTORY
        inv_path.write_bytes(inv)
        tar.add(inv_path, INVENTORY, recursive=False)
    return target


@dataclass
class VerifyReport:
    ok: bool = True
    files: int = 0
    blobs_checked: int = 0
    project_id: str | None = None
    problems: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.problems.append(msg)


def _safe_member(m: tarfile.TarInfo) -> bool:
    p = PurePosixPath(m.name)
    return m.isfile() and not p.is_absolute() and ".." not in p.parts


def verify_backup(path: Path) -> VerifyReport:
    """Checks the inventory, every file hash, blob content addresses and journal integrity. No GPU, no models."""
    rep = VerifyReport()
    with tarfile.open(path, "r") as tar, tempfile.TemporaryDirectory() as tmp:
        members = {m.name: m for m in tar.getmembers()}
        for name, m in members.items():
            if not _safe_member(m):
                rep.fail(f"unsafe member {name!r}")
        if INVENTORY not in members:
            rep.fail("missing inventory")
            return rep
        inv = json.loads(tar.extractfile(members[INVENTORY]).read())  # type: ignore[union-attr]
        if inv.get("format") != FORMAT:
            rep.fail(f"unknown backup format {inv.get('format')!r}")
            return rep
        rep.project_id = inv.get("project_id")
        listed = {e["path"] for e in inv["files"]}
        for extra in set(members) - listed - {INVENTORY}:
            rep.fail(f"undeclared member {extra}")
        for e in inv["files"]:
            m = members.get(e["path"])
            if m is None:
                rep.fail(f"missing {e['path']}")
                continue
            h = hashlib.sha256()
            f = tar.extractfile(m)
            assert f is not None
            for chunk in iter(lambda f=f: f.read(_CHUNK), b""):
                h.update(chunk)
            rep.files += 1
            digest = h.hexdigest()
            if digest != e["sha256"] or m.size != e["size"]:
                rep.fail(f"hash/size mismatch {e['path']}")
            if e["path"].startswith("project/blobs/sha256/"):
                rep.blobs_checked += 1
                if PurePosixPath(e["path"]).name != digest:
                    rep.fail(f"blob content does not match its address: {e['path']}")
        if "journal.sqlite" in members:
            jpath = Path(tmp) / "journal.sqlite"
            jpath.write_bytes(tar.extractfile(members["journal.sqlite"]).read())  # type: ignore[union-attr]
            con = sqlite3.connect(jpath)
            try:
                res = con.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                con.close()
            if res != "ok":
                rep.fail(f"journal integrity: {res}")
        if "project/studio.yaml" not in members:
            rep.fail("project/studio.yaml missing")
    return rep
