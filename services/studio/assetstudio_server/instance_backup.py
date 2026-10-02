"""Instance backup: integration server identity, bearer-token stores (hashes only) and a consistent journal snapshot.

Complements the per-project backup (packages/assetstudio_storage/backup.py). Studio must be stopped; the journal is
snapshotted with SQLite's online-backup API, never a raw file copy. Restoring is a documented manual procedure.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .secure_files import exclusive
from .settings import Settings

FORMAT = "assetstudio-instance-backup/1"
MANIFEST = "backup_manifest.json"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_CHUNK = 1 << 20


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _token_files(s: Settings) -> list[tuple[str, Path]]:
    """Explicit list of persistent auth state. Signed-URL/actor secrets are per-process."""
    return [("integration/server.json", s.integration_dir / "server.json"),
            ("integration/tokens.json", s.integration_dir / "tokens.json"),
            ("mcp_tokens.json", s.instance_dir / "mcp_tokens.json"),
            ("projects.json", s.instance_dir / "projects.json")]


def _stage(src: Path, dst: Path, *, locked: bool) -> None:
    if locked:
        with exclusive(src):  # a token writer mid-replace can never be captured half-way
            shutil.copyfile(src, dst)
    else:
        shutil.copyfile(src, dst)


def _snapshot_journal(src: Path, dst: Path) -> None:
    a, b = sqlite3.connect(src), sqlite3.connect(dst)
    try:
        with b:
            a.backup(b)
    finally:
        a.close()
        b.close()


def _server_id(path: Path) -> str | None:
    return json.loads(path.read_text())["server_id"] if path.is_file() else None


def create_instance_backup(s: Settings, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = out_dir / f"instance-{stamp}.tar.gz"
    members: dict[str, dict[str, object]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        staged: dict[str, Path] = {}
        for i, (arc, src) in enumerate(_token_files(s)):
            if src.is_file():
                staged[arc] = Path(tmp) / f"{i}.bin"
                # server.json and projects.json are replaced atomically and have no writer lock file
                _stage(src, staged[arc], locked=arc.endswith("tokens.json"))
        # Node mode: auth.sqlite holds runner credentials, registration tokens and the Studio offer-signing keys.
        for arc, db in (("journal/operations.sqlite", s.instance_dir / "journal" / "operations.sqlite"),
                        ("auth.sqlite", s.instance_dir / "auth.sqlite")):
            if db.is_file():
                staged[arc] = Path(tmp) / arc.replace("/", "_")
                _snapshot_journal(db, staged[arc])
        for arc, p in staged.items():
            members[arc] = {"sha256": _sha(p), "size": p.stat().st_size}
        manifest = Path(tmp) / MANIFEST
        manifest.write_text(json.dumps({
            "format": FORMAT, "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "server_id": _server_id(staged["integration/server.json"]) if "integration/server.json" in staged else None,
            "members": members}, indent=2))
        staged[MANIFEST] = manifest
        _write_tar(target, staged)
    return target


def _write_tar(target: Path, staged: dict[str, Path]) -> None:
    with target.open("xb") as raw:
        target.chmod(0o600)  # token hashes and revocations: owner only
        with tarfile.open(fileobj=raw, mode="w:gz") as tar:
            for arc, p in staged.items():
                ti = tarfile.TarInfo(arc)
                ti.size, ti.mode, ti.mtime = p.stat().st_size, 0o600, int(time.time())
                with p.open("rb") as f:
                    tar.addfile(ti, f)


@dataclass
class InstanceVerifyReport:
    ok: bool = True
    server_id: str | None = None
    members: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.problems.append(msg)


def _safe(m: tarfile.TarInfo) -> bool:
    p = PurePosixPath(m.name)
    return m.isfile() and not p.is_absolute() and ".." not in p.parts


def verify_instance_backup(path: Path) -> InstanceVerifyReport:
    rep = InstanceVerifyReport()
    try:
        tar = tarfile.open(path, "r:gz")
    except (tarfile.TarError, OSError) as e:
        rep.fail(f"unreadable archive: {e}")
        return rep
    with tar, tempfile.TemporaryDirectory() as tmp:
        names = {m.name: m for m in tar.getmembers()}
        for name, m in names.items():
            if not _safe(m):
                rep.fail(f"unsafe member {name!r}")
        if rep.problems:
            return rep
        manifest = _manifest(tar, names, rep)
        if manifest is None:
            return rep
        rep.server_id = manifest.get("server_id")
        listed: dict[str, dict] = manifest.get("members", {})
        for extra in set(names) - set(listed) - {MANIFEST}:
            rep.fail(f"undeclared member {extra}")
        for name, meta in listed.items():
            _check_member(tar, names, name, meta, Path(tmp), rep)
        rep.members = sorted(listed)
        _check_semantics(Path(tmp), listed, rep)
    return rep


def _manifest(tar: tarfile.TarFile, names: dict[str, tarfile.TarInfo], rep: InstanceVerifyReport) -> dict | None:
    if MANIFEST not in names:
        rep.fail("missing backup_manifest.json")
        return None
    try:
        doc = json.loads(tar.extractfile(names[MANIFEST]).read())  # type: ignore[union-attr]
    except ValueError:
        rep.fail("manifest is not valid JSON")
        return None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT or not isinstance(doc.get("members"), dict):
        rep.fail(f"unknown or malformed manifest (format {doc.get('format') if isinstance(doc, dict) else None!r})")
        return None
    return doc


def _check_member(tar: tarfile.TarFile, names: dict[str, tarfile.TarInfo], name: str, meta: dict, tmp: Path,
                  rep: InstanceVerifyReport) -> None:
    m = names.get(name)
    if m is None:
        rep.fail(f"missing {name}")
        return
    dst = tmp / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    with tar.extractfile(m) as src, dst.open("wb") as out:  # type: ignore[union-attr]
        shutil.copyfileobj(src, out)
    if _sha(dst) != meta.get("sha256") or dst.stat().st_size != meta.get("size"):
        rep.fail(f"hash/size mismatch {name}")


def _check_semantics(tmp: Path, listed: dict[str, dict], rep: InstanceVerifyReport) -> None:
    for name in listed:
        p = tmp / name
        if not p.is_file():
            continue
        if name == "integration/server.json":
            _check_identity(p, rep)
        elif name.endswith("tokens.json"):
            try:
                if not isinstance(json.loads(p.read_text()).get("tokens"), list):
                    rep.fail(f"{name}: 'tokens' is not a list")
            except (ValueError, AttributeError):
                rep.fail(f"{name}: not a token store")
        elif name == "projects.json":
            try:
                if not isinstance(json.loads(p.read_text()).get("projects"), list):
                    rep.fail("projects.json: 'projects' is not a list")
            except (ValueError, AttributeError):
                rep.fail("projects.json: not a project registry")
        elif name in ("journal/operations.sqlite", "auth.sqlite"):
            con = sqlite3.connect(p)
            try:
                res = con.execute("PRAGMA integrity_check").fetchone()[0]
            except sqlite3.DatabaseError as e:
                res = str(e)
            finally:
                con.close()
            if res != "ok":
                rep.fail(f"{name} integrity: {res}")


def _check_identity(p: Path, rep: InstanceVerifyReport) -> None:
    try:
        sid = json.loads(p.read_text()).get("server_id")
    except (ValueError, AttributeError):
        rep.fail("integration/server.json: not valid JSON")
        return
    if not isinstance(sid, str) or not _UUID.fullmatch(sid):
        rep.fail("integration/server.json: server_id is not a canonical UUID")
    elif sid != rep.server_id:
        rep.fail("integration/server.json: server_id differs from the manifest")
