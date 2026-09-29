# /// script
# requires-python = ">=3.10"
# dependencies = ["huggingface_hub[hf_xet]==0.36.0", "pyyaml==6.0.3"]
# ///
"""Explicit model download from config/models.lock.yaml (exact repo revision + exact file list). Never at runtime.

Gated models whose access is still pending are reported and skipped; dependent recipes stay disabled.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml
from huggingface_hub import snapshot_download
from huggingface_hub.errors import GatedRepoError, HfHubHTTPError

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="model keys (default: all non-optional)")
    ap.add_argument("--with-optional", action="store_true")
    ap.add_argument("--models-root", default=str(ROOT / "models"))
    args = ap.parse_args()
    lock = yaml.safe_load((ROOT / "config" / "models.lock.yaml").read_text())
    token = os.environ.get("HF_TOKEN") or None
    root = Path(args.models_root)
    results, failed = [], False
    for key, spec in lock["models"].items():
        if args.only and key not in args.only:
            continue
        if spec.get("optional") and not args.with_optional and not args.only:
            results.append((key, "skipped (optional)"))
            continue
        files = sorted(spec["files"])
        gb = sum(f.get("size") or 0 for f in spec["files"].values()) / 2**30
        print(f"==> {key}: {spec['repo']}@{spec['revision'][:10]} ({len(files)} files, {gb:.1f} GiB)", flush=True)
        try:
            snapshot_download(repo_id=spec["repo"], revision=spec["revision"], local_dir=root / spec["local_dir"],
                              allow_patterns=files, token=token)
            results.append((key, "downloaded"))
        except GatedRepoError:
            results.append((key, "PENDING: gated repo — accept the licence on Hugging Face and wait for approval"))
        except (HfHubHTTPError, OSError) as e:
            results.append((key, f"FAILED: {type(e).__name__}: {str(e)[:200]}"))
            failed = True
    print("\nSummary:")
    for key, status in results:
        print(f"  {key:28} {status}")
    print("\nNext: `make verify` (size check) or `make verify-full` (sha256).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
