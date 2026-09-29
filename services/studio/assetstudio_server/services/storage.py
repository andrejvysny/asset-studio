"""Storage view: measured statistics (never fixed demo numbers), connectivity test, layout."""
from __future__ import annotations

import secrets
from typing import Any

from assetstudio_core.domain import AssetManifest, AssetVersion
from assetstudio_storage.local import LocalBackend
from assetstudio_storage.project import manifest_key, version_key
from assetstudio_storage.repo import StorageError

from ..registry import ProjectContext

LAYOUT = """studio.yaml              schema · pipelines · qa · style · exports (no secrets)
shotlist.yaml            requested assets
config/revisions/        immutable effective-config snapshots (sha256)
manifests/<asset>.json   versions + current pointer
versions/<asset>/        immutable version records
artifacts/<id>.json      role + provenance -> content hash
blobs/sha256/ab/cd/…     immutable bytes, named by their hash
batches/<batch>/         items · prompts · candidate sets · QA · decisions · builds
_control/                writer ownership"""


def storage_view(ctx: ProjectContext) -> dict[str, Any]:
    backend = ctx.store.repo
    stats: dict[str, Any] = {"assets": 0, "versions": 0, "blobs": 0, "blob_bytes": 0, "logical_bytes": 0}
    if isinstance(backend, LocalBackend):
        blobs = backend.iter_blobs()
        stats["blobs"], stats["blob_bytes"] = len(blobs), sum(s for _, s in blobs)
        staging = backend.root / "blobs" / ".staging"
        stats["staging_bytes"] = sum(p.stat().st_size for p in staging.glob("*") if p.is_file()) \
            if staging.is_dir() else 0
    for asset_id in ctx.store.list_ids("manifests"):
        m, _ = ctx.store.get(manifest_key(asset_id), AssetManifest)
        stats["assets"] += 1
        stats["versions"] += len(m.versions)
        for v in m.versions:
            rec, _ = ctx.store.get(version_key(asset_id, v.version_id), AssetVersion)
            stats["logical_bytes"] += sum(int(a.get("size", 0)) for a in rec.artifacts.values())
    cfg, _ = ctx.config()
    return {
        "backend": backend.describe(),
        "state": "read_only" if ctx.read_only else "local",
        "owner": ctx.owner,
        "stats": stats,
        "retention": cfg.retention.model_dump(),
        "layout": LAYOUT,
        "s3": {"available": False, "reason": "S3-compatible storage lands in Phase 4 (conditional writes, "
                                             "writer fencing, migration); not simulated here"},
        "gc": {"available": False, "reason": "orphan collection (plan → preview → confirm) lands in Phase 4"},
    }


def test_storage(ctx: ProjectContext) -> dict[str, Any]:
    """Writes, reads, lists and deletes one sentinel under _control/test/ only."""
    repo = ctx.store.repo
    key = f"_control/test/sentinel-{secrets.token_hex(6)}"
    results: dict[str, Any] = {}
    try:
        results["write"] = bool(repo.create_if_absent(key, b"assetstudio sentinel"))
        results["read"] = repo.read_object(key).data == b"assetstudio sentinel"
        results["list"] = key in repo.list_keys("_control/test")[0]
        repo.delete_object(key)
        results["delete"] = True
    except StorageError as e:
        results["error"] = f"{e.code}: {e}"
    results["ok"] = all(results.get(k) for k in ("write", "read", "list", "delete"))
    return results
