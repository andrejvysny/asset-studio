"""Per-asset publication (spec §13.3): blobs verified -> immutable version record -> conditional manifest update.

The manifest commit is the publication point. Ids derive from the publication op id, so a retry after a crash
reproduces the same asset/version identities instead of duplicating them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.contracts import check_roles
from assetstudio_core.domain import AssetManifest, AssetVersion, PointerChange, VersionRef
from assetstudio_core.ids import derived_id
from assetstudio_core.kinds import Kind, Origin

from .project import ProjectStore, manifest_key, version_key
from .repo import Conflict, IntegrityError, NotFound, StorageError


class StalePointer(Conflict):
    code = "stale_pointer"


class KindMismatch(Conflict):
    code = "kind_mismatch"


class NameTaken(Conflict):
    code = "name_taken"


class RoleContractViolation(StorageError):
    code = "role_contract"


def name_key(name_id: str) -> str:
    return f"names/{name_id}.json"


def receipt_key(op_id: str) -> str:
    return f"publications/{op_id}.json"


def reserve_name(store: ProjectStore, name_id: str, asset_id: str) -> None:
    """Authoritative readable-name uniqueness: an atomic create-if-absent per name (the index is only a cache)."""
    try:
        store.create(name_key(name_id), {"name_id": name_id, "asset_id": asset_id, "at": now_iso()})
    except Conflict as e:
        held = store.repo.read_object(name_key(name_id))
        if json.loads(held.data).get("asset_id") != asset_id:
            raise NameTaken(f"name {name_id!r} belongs to another asset") from e


def backfill_names(store: ProjectStore) -> int:
    """Projects written before name records existed: reserve every manifest's name (idempotent)."""
    n = 0
    for asset_id in store.list_ids("manifests"):
        m, _ = store.get(manifest_key(asset_id), AssetManifest)
        if store.repo.stat_object(name_key(m.name_id)) is None:
            reserve_name(store, m.name_id, m.asset_id)
            n += 1
    return n


@dataclass
class NewAsset:
    name_id: str
    display_name: str
    kind: Kind
    origin: Origin
    category_id: str | None
    tags: list[str] = field(default_factory=list)


@dataclass
class PublishRequest:
    op_id: str
    idempotency_key: str
    artifacts: dict[str, str]  # role -> artifact id
    preview_role: str | None
    origin: Origin
    asset_id: str | None = None  # existing asset, or None with new_asset
    new_asset: NewAsset | None = None
    expected_current_version: str | None = None
    make_current: bool = True
    details: dict[str, Any] = field(default_factory=dict)  # sources/config/models/engine/qa/validation/licence
    note: str = ""
    kind: Kind | None = None  # the produced kind: must equal the target asset's kind (checked at commit)


@dataclass
class PublishResult:
    asset_id: str
    version_id: str
    display_version: int
    created: bool


def _verified_artifacts(store: ProjectStore, roles: dict[str, str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for role, art_id in roles.items():
        art = store.artifact(art_id)
        try:
            store.repo.verify_blob(art.sha256, art.size)  # full hash (identity-cached), not existence/size
        except NotFound as e:
            raise IntegrityError(f"blob for {role} ({art.sha256[:12]}) is missing") from e
        out[role] = {"artifact_id": art.id, "sha256": art.sha256, "size": art.size, "mime": art.mime}
    return out


def publish(store: ProjectStore, req: PublishRequest) -> PublishResult:
    if (req.asset_id is None) == (req.new_asset is None):
        raise StorageError("give exactly one of asset_id / new_asset")
    asset_id = req.asset_id or derived_id("ast", store.project_id, req.op_id)
    version_id = derived_id("ver", asset_id, req.op_id)
    with store.lock:
        manifest, token = store.get_opt(manifest_key(asset_id), AssetManifest)
        if manifest is not None:
            done = next((v for v in manifest.versions if v.publication_op == req.op_id), None)
            if done is not None:
                store.create_or_same(receipt_key(req.op_id), {
                    "op_id": req.op_id, "asset_id": asset_id, "version_id": done.version_id,
                    "display_version": done.display_version})
                return PublishResult(asset_id, done.version_id, done.display_version, False)
            if req.new_asset is not None:
                raise Conflict(f"asset {asset_id} already exists")
            if manifest.current_version_id != req.expected_current_version:
                raise StalePointer(
                    f"current version is {manifest.current_version_id}, expected {req.expected_current_version}")
        elif req.asset_id is not None:
            raise NotFound(f"asset {req.asset_id}")
        kind = req.new_asset.kind if req.new_asset else manifest.kind  # type: ignore[union-attr]
        if req.kind is not None and req.kind != kind:
            raise KindMismatch(f"a {req.kind.value} result cannot become a version of a {kind.value} asset")
        if problems := check_roles(kind, set(req.artifacts)):
            raise RoleContractViolation("; ".join(problems))
        if req.new_asset is not None:
            reserve_name(store, req.new_asset.name_id, asset_id)

        artifacts = _verified_artifacts(store, req.artifacts)
        display_version = 1 + max((v.display_version for v in manifest.versions), default=0) if manifest else 1
        existing_version, _ = store.get_opt(version_key(asset_id, version_id), AssetVersion)
        now = now_iso()
        if existing_version is None:
            base = req.new_asset or NewAsset(manifest.name_id, manifest.display_name, manifest.kind,  # type: ignore
                                             manifest.origin, manifest.category_id, manifest.tags)  # type: ignore
            version = AssetVersion(
                asset_id=asset_id, version_id=version_id, display_version=display_version,
                project_id=store.project_id, kind=base.kind, origin=req.origin, display_name=base.display_name,
                category_id=base.category_id, tags=base.tags, artifacts=artifacts,
                sources=req.details.get("sources", {}), config_snapshot_sha=req.details.get("config_snapshot_sha"),
                models=req.details.get("models", []), engine=req.details.get("engine", {}),
                parameters=req.details.get("parameters", {}), qa=req.details.get("qa"),
                validation=req.details.get("validation", {}), licence=req.details.get("licence", {}),
                publication={"op_id": req.op_id, "idempotency_key": req.idempotency_key, "published_at": now,
                             "expected_previous_version": req.expected_current_version},
                note=req.note,
            )
            store.create(version_key(asset_id, version_id), version)
        else:
            if existing_version.publication.get("op_id") != req.op_id:
                raise Conflict(f"version {version_id} belongs to another publication")
            version, display_version = existing_version, existing_version.display_version
        preview = artifacts.get(req.preview_role or "", {}).get("artifact_id")
        ref = VersionRef(version_id=version_id, display_version=display_version,
                         published_at=version.publication["published_at"], publication_op=req.op_id,
                         preview_artifact_id=preview, note=req.note)
        if manifest is None:
            assert req.new_asset is not None
        make_current = req.make_current or manifest is None
        pointer = [PointerChange(from_version=manifest.current_version_id if manifest else None, to_version=version_id,
                                 at=now, op=req.op_id, reason="publish")] if make_current else []
        if manifest is None:
            na = req.new_asset
            assert na is not None
            new = AssetManifest(asset_id=asset_id, name_id=na.name_id, display_name=na.display_name, kind=na.kind,
                                origin=req.origin, category_id=na.category_id, tags=na.tags, created_at=now,
                                current_version_id=version_id, versions=[ref], pointer_log=pointer)
            store.create(manifest_key(asset_id), new)
        else:
            assert token is not None
            manifest.versions.append(ref)
            manifest.pointer_log += pointer
            if make_current:
                manifest.current_version_id = version_id
            if manifest.origin != req.origin:
                manifest.origin = Origin.mixed
            manifest.revision += 1
            store.replace(manifest_key(asset_id), manifest, token)
        # Durable receipt after the publication point: replay finds the same result without guessing.
        store.create_or_same(receipt_key(req.op_id), {"op_id": req.op_id, "asset_id": asset_id,
                                                      "version_id": version_id, "display_version": display_version})
        return PublishResult(asset_id, version_id, display_version, True)


def set_current(store: ProjectStore, asset_id: str, version_id: str, expected_current: str | None, op_id: str,
                reason: str = "") -> AssetManifest:
    with store.lock:
        manifest, token = store.get(manifest_key(asset_id), AssetManifest)
        if any(p.op == op_id for p in manifest.pointer_log):
            return manifest
        if manifest.version(version_id) is None:
            raise NotFound(f"version {version_id} of {asset_id}")
        if manifest.current_version_id != expected_current:
            raise StalePointer(f"current version is {manifest.current_version_id}, expected {expected_current}")
        manifest.pointer_log.append(PointerChange(from_version=manifest.current_version_id, to_version=version_id,
                                                  at=now_iso(), op=op_id, reason=reason or "set current"))
        manifest.current_version_id = version_id
        manifest.revision += 1
        store.replace(manifest_key(asset_id), manifest, token)
        return manifest


def update_metadata(store: ProjectStore, asset_id: str, expected_revision: int, *, display_name: str | None = None,
                    category_id: str | None = None, tags: list[str] | None = None,
                    set_category: bool = False) -> AssetManifest:
    """Mutable classification only. Never touches versions or content."""
    with store.lock:
        manifest, token = store.get(manifest_key(asset_id), AssetManifest)
        if manifest.revision != expected_revision:
            raise Conflict(f"asset changed (revision {manifest.revision})")
        if display_name is not None:
            manifest.display_name = display_name
        if set_category:
            manifest.category_id = category_id
        if tags is not None:
            manifest.tags = sorted(set(tags))
        manifest.revision += 1
        store.replace(manifest_key(asset_id), manifest, token)
        return manifest
