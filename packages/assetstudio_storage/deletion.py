"""Permanent asset deletion. Only an archived asset can be deleted, and only when no other record refers to it.

Reference-aware by scanning every record key (never blobs) for the asset id, its version ids, its artifact ids and
their blob digests, so jobs, families, deliveries, publications and variants are found without this module knowing
their schemas. Cleanup is ordered (artifacts and blobs, then versions, then the manifest last) and tolerant of
missing objects: a crash mid-way leaves the asset archived and a repeated call finishes the job.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from assetstudio_core.domain import Artifact, AssetManifest, AssetVersion

from .project import ProjectStore, artifact_key, manifest_key, version_key
from .publication import name_key
from .repo import Conflict, NotFound

# Per-asset bookkeeping that dies with the asset; every other record mentioning the asset blocks deletion.
_OWNED_PREFIXES = ("names/", "publications/", "imports/")


class AssetNotArchived(Conflict):
    code = "asset_not_archived"


class AssetInUse(Conflict):
    code = "asset_in_use"

    def __init__(self, blockers: list[dict[str, str]]) -> None:
        super().__init__(f"asset is referenced by {len(blockers)} record(s)")
        self.blockers = blockers


@dataclass
class DeleteResult:
    asset_id: str
    versions: int = 0
    artifacts_deleted: list[str] = field(default_factory=list)
    artifacts_kept: list[str] = field(default_factory=list)  # still referenced by another record
    blobs_deleted: int = 0
    blobs_kept: int = 0  # digest still used by another artifact or record

    def as_dict(self) -> dict[str, Any]:
        return {"asset_id": self.asset_id, "versions": self.versions, "artifacts_deleted": len(self.artifacts_deleted),
                "artifacts_kept": len(self.artifacts_kept), "blobs_deleted": self.blobs_deleted,
                "blobs_kept": self.blobs_kept}


def _load_versions(store: ProjectStore, manifest: AssetManifest) -> list[AssetVersion]:
    out: list[AssetVersion] = []
    for ref in manifest.versions:
        v, _ = store.get_opt(version_key(manifest.asset_id, ref.version_id), AssetVersion)
        if v is not None:  # absent after an interrupted earlier delete
            out.append(v)
    return out


def _artifact_ids(manifest: AssetManifest, versions: list[AssetVersion]) -> list[str]:
    ids = {a["artifact_id"] for v in versions for a in v.artifacts.values()}
    ids |= {r.preview_artifact_id for r in manifest.versions if r.preview_artifact_id}
    return sorted(ids)


@dataclass
class _Scan:
    blockers: list[dict[str, str]] = field(default_factory=list)
    owned: list[str] = field(default_factory=list)
    artifact_hits: set[str] = field(default_factory=set)
    sha_hits: set[str] = field(default_factory=set)


def _scan(store: ProjectStore, manifest: AssetManifest, art_ids: list[str], shas: dict[str, str]) -> _Scan:
    asset_ids = [manifest.asset_id.encode(), *(r.version_id.encode() for r in manifest.versions)]
    own = {manifest_key(manifest.asset_id), *(version_key(manifest.asset_id, r.version_id) for r in manifest.versions),
           *(artifact_key(a) for a in art_ids)}
    out = _Scan()
    for key in store.repo.scan_keys():
        if key in own:
            continue
        try:
            data = store.repo.read_object(key).data
        except NotFound:
            continue
        if any(n in data for n in asset_ids):
            if key.startswith(_OWNED_PREFIXES):
                out.owned.append(key)  # our own receipt: it names our artifacts but is not a reason to keep them
                continue
            out.blockers.append({"type": key.split("/", 1)[0], "key": key})
        out.artifact_hits |= {a for a in art_ids if a.encode() in data}
        out.sha_hits |= {s for s in set(shas.values()) if s.encode() in data}
    return out


def _delete_quiet(store: ProjectStore, key: str) -> None:
    try:
        store.repo.delete_object(key)
    except NotFound:
        pass


def delete_asset(store: ProjectStore, asset_id: str, expected_revision: int) -> DeleteResult:
    """Raises AssetNotArchived, Conflict (stale revision) or AssetInUse (with `.blockers`) before changing anything."""
    with store.lock:
        manifest, _ = store.get(manifest_key(asset_id), AssetManifest)
        if manifest.archived_at is None:
            raise AssetNotArchived(f"asset {asset_id} is not archived; archive it first")
        if manifest.revision != expected_revision:
            raise Conflict(f"asset changed (revision {manifest.revision})")
        versions = _load_versions(store, manifest)
        art_ids = _artifact_ids(manifest, versions)
        shas: dict[str, str] = {}
        for a in art_ids:
            art, _ = store.get_opt(artifact_key(a), Artifact)
            if art is not None:
                shas[a] = art.sha256
        scan = _scan(store, manifest, art_ids, shas)
        if scan.blockers:
            raise AssetInUse(scan.blockers)

        res = DeleteResult(asset_id, versions=len(manifest.versions))
        keep_shas = set(scan.sha_hits) | {shas[a] for a in scan.artifact_hits if a in shas}
        for a in art_ids:
            if a in scan.artifact_hits:
                res.artifacts_kept.append(a)
                continue
            _delete_quiet(store, artifact_key(a))
            res.artifacts_deleted.append(a)
        for sha in sorted({shas[a] for a in res.artifacts_deleted if a in shas}):
            if sha in keep_shas:
                res.blobs_kept += 1
            elif store.repo.delete_blob(sha):
                res.blobs_deleted += 1
        for ref in manifest.versions:
            _delete_quiet(store, version_key(asset_id, ref.version_id))
        for key in scan.owned:
            _delete_quiet(store, key)
        _delete_quiet(store, name_key(manifest.name_id))
        _delete_quiet(store, manifest_key(asset_id))  # last: until it is gone a repeated call can finish the job
        return res
