# /// script
# requires-python = ">=3.10"
# dependencies = ["huggingface_hub[hf_xet]==0.36.0", "pyyaml==6.0.2"]
# ///
"""Download pinned models from config/models.yaml and write per-model manifests."""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from pathlib import Path

import yaml
from huggingface_hub import HfApi, snapshot_download

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_DIR = ROOT / "models" / "manifests"


def remote_files(api: HfApi, repo: str, rev: str, include: list[str] | None) -> dict[str, dict]:
    info = api.model_info(repo, revision=rev, files_metadata=True)
    out: dict[str, dict] = {}
    for s in info.siblings or []:
        if include and not any(fnmatch.fnmatch(s.rfilename, p) for p in include):
            continue
        out[s.rfilename] = {"size": s.size, "sha256": s.lfs.sha256 if s.lfs else None}
    return out


def download(name: str, spec: dict, token: str | None) -> str:
    local_dir = ROOT / spec["local_dir"]
    include = spec.get("include")
    api = HfApi(token=token)
    files = remote_files(api, spec["repo"], spec["revision"], include)
    snapshot_download(
        repo_id=spec["repo"],
        revision=spec["revision"],
        local_dir=local_dir,
        allow_patterns=include,
        token=token,
    )
    manifest = {
        "name": name,
        "repo": spec["repo"],
        "revision": spec["revision"],
        "local_dir": spec["local_dir"],
        "files": files,
    }
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    (MANIFEST_DIR / f"{name}.json").write_text(json.dumps(manifest, indent=2))
    return f"{len(files)} files"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="model keys to download")
    ap.add_argument("--with-optional", action="store_true", help="include optional models")
    args = ap.parse_args()

    models: dict[str, dict] = yaml.safe_load((ROOT / "config" / "models.yaml").read_text())["models"]
    token = os.environ.get("HF_TOKEN") or None  # None -> cached `hf auth login` token
    results: list[tuple[str, str]] = []
    failed = False
    for name, spec in models.items():
        if args.only and name not in args.only:
            continue
        if spec.get("optional") and not args.with_optional and not args.only:
            results.append((name, "skipped (optional)"))
            continue
        print(f"==> {name}: {spec['repo']}@{spec['revision'][:10]}", flush=True)
        try:
            results.append((name, download(name, spec, token)))
        except Exception as e:  # report all models, don't stop at first failure
            hint = " (gated: accept license on HF, set HF_TOKEN or `hf auth login`)" if spec.get("gated") else ""
            results.append((name, f"FAILED{hint}: {type(e).__name__}: {str(e)[:200]}"))
            failed = True

    print("\nSummary:")
    for name, status in results:
        print(f"  {name:28s} {status}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
