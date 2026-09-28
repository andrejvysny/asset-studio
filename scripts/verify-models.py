# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml==6.0.2"]
# ///
"""Offline check of downloaded models against models/manifests/*.json (size; sha256 with --full)."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 24), b""):
            h.update(chunk)
    return h.hexdigest()


def check(name: str, full: bool) -> list[str]:
    mpath = ROOT / "models" / "manifests" / f"{name}.json"
    if not mpath.exists():
        return ["no manifest (not downloaded?)"]
    manifest = json.loads(mpath.read_text())
    base = ROOT / manifest["local_dir"]
    errors: list[str] = []
    for rel, meta in manifest["files"].items():
        p = base / rel
        if not p.is_file():
            errors.append(f"missing {rel}")
        elif meta["size"] is not None and p.stat().st_size != meta["size"]:
            errors.append(f"size mismatch {rel}")
        elif full and meta.get("sha256") and sha256(p) != meta["sha256"]:
            errors.append(f"sha256 mismatch {rel}")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="verify sha256 of LFS files (slow)")
    args = ap.parse_args()
    models: dict[str, dict] = yaml.safe_load((ROOT / "config" / "models.yaml").read_text())["models"]
    bad = False
    for name, spec in models.items():
        errors = check(name, args.full)
        if not errors:
            print(f"  OK       {name}")
        elif spec.get("optional"):
            print(f"  OPTIONAL {name}: {errors[0]}")
        else:
            bad = True
            print(f"  FAIL     {name}: {'; '.join(errors[:3])}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
