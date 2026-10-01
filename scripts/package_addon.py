"""Build the AssetStudio Godot addon zip for consumers (godot-ipad IP-03, desktop projects).

Usage: python3 scripts/package_addon.py [--out-dir dist]
Writes dist/assetstudio-addon-<version>.zip and a sidecar <zip>.sha256 (`<hex>  <name>`).
The archive holds addons/assetstudio/** plus the frozen v1 schemas, capabilities, error codes and README under
addons/assetstudio/contracts/v1/. Deterministic: sorted entries, fixed timestamps and modes, stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import re
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
SKIP_NAMES = {".DS_Store"}
SKIP_SUFFIXES = (".tmp",)


def read_version() -> str:
    m = re.search(r'^const VERSION: String = "([0-9A-Za-z._+-]+)"$', VERSION_FILE.read_text(), re.M)
    if not m:
        raise SystemExit(f"cannot read VERSION from {VERSION_FILE}")
    return m.group(1)


def collect() -> dict[str, Path]:
    entries: dict[str, Path] = {}
    for p in sorted(ADDON.rglob("*")):
        if p.is_file() and p.name not in SKIP_NAMES and not p.name.endswith(SKIP_SUFFIXES):
            entries[f"{ARCHIVE_PREFIX}/{p.relative_to(ADDON).as_posix()}"] = p
    contract_paths = sorted(CONTRACTS.glob("*.schema.json")) + [CONTRACTS / n for n in CONTRACT_FILES]
    for p in contract_paths:
        entries[f"{ARCHIVE_PREFIX}/contracts/v1/{p.name}"] = p
    return dict(sorted(entries.items()))


def build(out_dir: Path) -> tuple[Path, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"assetstudio-addon-{read_version()}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name, src in collect().items():
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            zf.writestr(info, src.read_bytes())
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    zip_path.with_name(zip_path.name + ".sha256").write_text(f"{digest}  {zip_path.name}\n")
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
