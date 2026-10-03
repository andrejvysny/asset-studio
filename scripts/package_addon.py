"""Build the AssetStudio Godot addon zip for consumers (godot-ipad IP-03, desktop projects).

Usage: python3 scripts/package_addon.py [--out-dir dist]
Writes dist/assetstudio-addon-<version>.zip, a sidecar <zip>.sha256 (`<hex>  <name>`) and
assetstudio-addon-<version>.manifest.json (version, source commit + dirty flag, contract version, per-file sha256).
The archive holds addons/assetstudio/** plus the frozen v1 schemas, capabilities, error codes and README under
addons/assetstudio/contracts/v1/. Deterministic: sorted entries, fixed timestamps and modes, stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "integrations" / "godot" / "addons" / "assetstudio"
CONTRACTS = ROOT / "contracts" / "godot-integration" / "v1"
VERSION_FILE = ADDON / "core" / "as_version.gd"
ARCHIVE_PREFIX = "addons/assetstudio"
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
CONTRACT_FILES = ("capabilities.json", "error-codes.json", "README.md")
SKIP_NAMES = {".DS_Store", ".env", "connections.json"}
SKIP_SUFFIXES = (".tmp", ".pyc", ".token", ".secret", ".key", ".pem")
# Never packaged, wherever they sit under the addon: tests, editor cache, tool state, caches, VCS, credentials.
SKIP_DIRS = {".godot", ".claude", ".git", ".assetstudio", "__pycache__", "tests", "test", ".pytest_cache", "secrets"}


def read_version() -> str:
    m = re.search(r'^const VERSION: String = "([0-9A-Za-z._+-]+)"$', VERSION_FILE.read_text(), re.M)
    if not m:
        raise SystemExit(f"cannot read VERSION from {VERSION_FILE}")
    return m.group(1)


def read_contract_version() -> int:
    m = re.search(r"^const CONTRACT_VERSION: int = ([0-9]+)$", VERSION_FILE.read_text(), re.M)
    if not m:
        raise SystemExit(f"cannot read CONTRACT_VERSION from {VERSION_FILE}")
    return int(m.group(1))


def _skipped(p: Path) -> bool:
    rel = p.relative_to(ADDON)
    return (any(part in SKIP_DIRS for part in rel.parts[:-1]) or p.name in SKIP_NAMES
            or p.name.endswith(SKIP_SUFFIXES))


def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return r.stdout


def source_state() -> dict[str, object]:
    """HEAD commit and whether the packaged inputs differ from it (None when git is unavailable)."""
    head = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain", "--", str(ADDON), str(CONTRACTS), str(Path(__file__).resolve()))
    return {"commit": head.strip() if head else None, "dirty": None if status is None else bool(status.strip())}


def collect() -> dict[str, Path]:
    entries: dict[str, Path] = {}
    for p in sorted(ADDON.rglob("*")):
        if p.is_file() and not _skipped(p):
            entries[f"{ARCHIVE_PREFIX}/{p.relative_to(ADDON).as_posix()}"] = p
    contract_paths = sorted(CONTRACTS.glob("*.schema.json")) + [CONTRACTS / n for n in CONTRACT_FILES]
    for p in contract_paths:
        entries[f"{ARCHIVE_PREFIX}/contracts/v1/{p.name}"] = p
    return dict(sorted(entries.items()))


def build_manifest(version: str, files: dict[str, str], zip_name: str, zip_sha256: str) -> dict[str, object]:
    return {"schema_version": 1, "addon": "assetstudio", "version": version,
            "contract_version": read_contract_version(), "source": source_state(),
            "archive": {"name": zip_name, "sha256": zip_sha256}, "files": files}


def build(out_dir: Path) -> tuple[Path, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    version = read_version()
    zip_path = out_dir / f"assetstudio-addon-{version}.zip"
    hashes: dict[str, str] = {}
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name, src in collect().items():
            data = src.read_bytes()
            hashes[name] = hashlib.sha256(data).hexdigest()
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            zf.writestr(info, data)
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    zip_path.with_name(zip_path.name + ".sha256").write_text(f"{digest}  {zip_path.name}\n")
    manifest = build_manifest(version, hashes, zip_path.name, digest)
    (out_dir / f"assetstudio-addon-{version}.manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return zip_path, digest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", type=Path, default=ROOT / "dist")
    args = ap.parse_args()
    path, digest = build(args.out_dir)
    print(f"{digest}  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
