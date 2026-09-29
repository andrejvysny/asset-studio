"""Asset families: metadata + anchor records. Membership lives ONLY on AssetManifest.family_id (never a member list)."""
from __future__ import annotations

from assetstudio_core.canonical import now_iso
from assetstudio_core.domain import AssetFamily, AssetManifest
from assetstudio_core.kinds import Kind

from .project import ProjectStore, manifest_key
from .repo import Conflict


class FamilyConflict(Conflict):
    code = "source_family_changed"


class FamilyKindMismatch(Conflict):
    code = "family_kind_mismatch"


def family_key(fid: str) -> str:
    return f"families/{fid}.json"


def get_family(store: ProjectStore, fid: str) -> AssetFamily:
    return store.get(family_key(fid), AssetFamily)[0]


def list_families(store: ProjectStore) -> list[AssetFamily]:
    out = [f for fid in store.list_ids("families") if (f := store.get_opt(family_key(fid), AssetFamily)[0])]
    return sorted(out, key=lambda f: (f.name.lower(), f.id))


def require_family_kind(store: ProjectStore, family_id: str, kind: Kind) -> AssetFamily:
    """Read-only precondition shared with publication: a family only groups assets of its own kind."""
    fam = get_family(store, family_id)
    if fam.kind != kind:
        raise FamilyKindMismatch(f"family {family_id} holds {fam.kind.value} assets, not {kind.value}")
    return fam


def create_family(store: ProjectStore, *, family_id: str, name: str, kind: Kind, anchor_asset_id: str,
                  anchor_version_id: str, op_id: str) -> AssetFamily:
    """Create-or-same on the derived id: replay of the same op returns the record, another op's record conflicts."""
    with store.lock:
        existing, _ = store.get_opt(family_key(family_id), AssetFamily)
        if existing is not None:
            if existing.created_by_op != op_id:
                raise Conflict(f"family {family_id} was created by another operation")
            return existing
        now = now_iso()
        fam = AssetFamily(id=family_id, name=name, kind=kind, anchor_asset_id=anchor_asset_id,
                          anchor_version_id=anchor_version_id, created_at=now, updated_at=now, created_by_op=op_id)
        store.create(family_key(family_id), fam)
        return fam


def attach_asset(store: ProjectStore, *, asset_id: str, family_id: str,
                 expected_manifest_revision: int | None) -> AssetManifest:
    """Classification change only: sets family_id on an unassigned asset. Never moves membership or versions."""
    with store.lock:
        manifest, token = store.get(manifest_key(asset_id), AssetManifest)
        require_family_kind(store, family_id, manifest.kind)
        if manifest.family_id == family_id:
            return manifest
        if manifest.family_id is not None:
            raise FamilyConflict(f"asset {asset_id} already belongs to family {manifest.family_id}")
        if expected_manifest_revision is not None and manifest.revision != expected_manifest_revision:
            raise FamilyConflict(f"source changed (revision {manifest.revision}, expected "
                                 f"{expected_manifest_revision}); reload")
        manifest.family_id = family_id
        manifest.revision += 1
        store.replace(manifest_key(asset_id), manifest, token)
        return manifest


def update_family(store: ProjectStore, fid: str, expected_revision: int, *, name: str | None = None,
                  description: str | None = None) -> AssetFamily:
    with store.lock:
        fam, token = store.get(family_key(fid), AssetFamily)
        if fam.revision != expected_revision:
            raise Conflict(f"family changed (revision {fam.revision})")
        if name is not None:
            name = name.strip()
            if not name or len(name) > 120:
                raise ValueError("family name must be 1-120 characters")
            fam.name = name
        if description is not None:
            fam.description = description
        fam.revision += 1
        fam.updated_at = now_iso()
        store.replace(family_key(fid), fam, token)
        return fam

