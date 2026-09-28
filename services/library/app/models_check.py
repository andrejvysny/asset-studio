"""Installed-model check anchored to config/models.yaml (repo + revision + file coverage), not just the manifest."""
from __future__ import annotations

import json
from pathlib import Path

import yaml


def check_model(name: str, spec: dict, root: Path) -> dict:
    mpath = root / "models" / "manifests" / f"{name}.json"
    row = {"key": name, "repo": spec["repo"], "revision": spec["revision"][:8], "optional": bool(spec.get("optional")),
           "gated": bool(spec.get("gated")), "status": "ok", "note": ""}
    if not mpath.is_file():
        return {**row, "status": "missing", "note": "not downloaded"}
    m = json.loads(mpath.read_text())
    if m.get("repo") != spec["repo"] or m.get("revision") != spec["revision"]:
        return {**row, "status": "stale", "note": f"installed {m.get('repo')}@{str(m.get('revision'))[:8]}"}
    files = m.get("files") or {}
    if not files:
        return {**row, "status": "invalid", "note": "manifest lists no files"}
    base = root / spec["local_dir"]
    missing = [f for f, meta in files.items()
               if not (base / f).is_file() or (meta.get("size") is not None and (base / f).stat().st_size != meta["size"])]
    if missing:
        return {**row, "status": "incomplete", "note": f"{len(missing)} file(s) missing/size mismatch: {missing[0]}"}
    return {**row, "note": f"{len(files)} files"}


def check_all(root: Path) -> list[dict]:
    models = yaml.safe_load((root / "config" / "models.yaml").read_text())["models"]
    return [check_model(k, v, root) for k, v in models.items()]
