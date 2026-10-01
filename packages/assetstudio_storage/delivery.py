"""Immutable descriptor / delivery storage for the Godot integration (INT-SPEC §4). Storage only.

Everything here is create-if-absent: the first frozen descriptor of a version wins forever, a delivery id never
changes its bytes, and `versions/`, `manifests/` and existing artifacts are never touched.
"""
from __future__ import annotations

import hashlib
from typing import Any, Literal

from assetstudio_core.ids import derived_id
from pydantic import BaseModel, ConfigDict

from .project import ProjectStore
from .repo import Conflict, IntegrityError

DESCRIPTOR_ROLE = "integration_descriptor"
MANIFEST_ROLE = "integration_manifest"


class DescriptorRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str
    version_id: str
    descriptor_sha256: str
    artifact_id: str
    origin: Literal["published", "projection"]
    profile_id: str
    profile_version: str


class DeliveryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delivery_id: str
    asset_id: str
    version_id: str
    representation: str
    profile_id: str
    profile_version: str
    manifest_sha256: str
    manifest_artifact_id: str
    total_bytes: int
    budget: dict[str, Any]


def descriptor_key(asset_id: str, version_id: str) -> str:
    return f"descriptors/{asset_id}/{version_id}.json"


def delivery_key(asset_id: str, version_id: str, delivery_id: str) -> str:
    return f"deliveries/{asset_id}/{version_id}/{delivery_id}.json"


def delivery_index_key(delivery_id: str) -> str:
    return f"delivery_index/{delivery_id}.json"


def marker_key(artifact_id: str) -> str:
    return f"delivery_artifacts/{artifact_id}.json"


def freeze_descriptor(store: ProjectStore, asset_id: str, version_id: str, raw: bytes,
                      origin: Literal["published", "projection"], profile_id: str,
                      profile_version: str) -> DescriptorRecord:
    """First freeze wins: later code/profile versions never change what a version's descriptor says."""
    art_id = derived_id("art", store.project_id, asset_id, version_id, "descriptor")
    try:
        art = store.register_artifact(raw, DESCRIPTOR_ROLE, "application/json", artifact_id=art_id)
    except Conflict:  # crash after an earlier freeze registered different bytes: that freeze still wins
        art = store.artifact(art_id)
    record = DescriptorRecord(asset_id=asset_id, version_id=version_id, descriptor_sha256=art.sha256,
                              artifact_id=art.id, origin=origin, profile_id=profile_id,
                              profile_version=profile_version)
    try:
        store.create(descriptor_key(asset_id, version_id), record)
    except Conflict:
        return store.get(descriptor_key(asset_id, version_id), DescriptorRecord)[0]
    return record


def put_delivery(store: ProjectStore, fields: dict[str, Any], manifest_raw: bytes,
                 artifact_ids: list[str]) -> tuple[DeliveryRecord, bool]:
    """Store a delivery; returns (record, created). Same id with different manifest bytes is corruption."""
    delivery_id = fields["delivery_id"]
    try:
        art = store.register_artifact(manifest_raw, MANIFEST_ROLE, "application/json",
                                      artifact_id=derived_id("art", store.project_id, delivery_id, "manifest"))
    except Conflict:
        raise IntegrityError(f"integrity_mismatch: delivery {delivery_id} already holds a different manifest "
                             "(profile version not bumped?)") from None
    record = DeliveryRecord(**fields, manifest_sha256=art.sha256, manifest_artifact_id=art.id)
    key = delivery_key(record.asset_id, record.version_id, delivery_id)
    created = True
    try:
        store.create(key, record)
    except Conflict:
        created = False
        existing = store.get(key, DeliveryRecord)[0]
        if existing.manifest_sha256 != record.manifest_sha256:
            raise IntegrityError(f"integrity_mismatch: delivery {delivery_id} already holds a different manifest "
                                 "(profile version not bumped?)") from None
        record = existing
    store.create_or_same(delivery_index_key(delivery_id),
                         {"asset_id": record.asset_id, "version_id": record.version_id})
    for artifact_id in artifact_ids:
        try:
            store.create(marker_key(artifact_id), {"asset_id": record.asset_id, "version_id": record.version_id,
                                                   "delivery_id": delivery_id})
        except Conflict:
            pass  # first delivery referencing the artifact keeps the marker
    return record, created


def descriptor(store: ProjectStore, asset_id: str, version_id: str) -> tuple[DescriptorRecord, bytes] | None:
    rec, _ = store.get_opt(descriptor_key(asset_id, version_id), DescriptorRecord)
    if rec is None:
        return None
    raw = store.artifact_bytes(rec.artifact_id, max_bytes=16 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != rec.descriptor_sha256:
        raise IntegrityError(f"integrity_mismatch: descriptor of {version_id} does not match its record")
    return rec, raw


def deliveries(store: ProjectStore, asset_id: str, version_id: str) -> list[DeliveryRecord]:
    prefix = f"deliveries/{asset_id}/{version_id}"
    out = [store.get(delivery_key(asset_id, version_id, d), DeliveryRecord)[0] for d in store.list_ids(prefix)]
    return sorted(out, key=lambda r: (r.representation, r.delivery_id))


def delivery_by_id(store: ProjectStore, delivery_id: str) -> DeliveryRecord | None:
    idx, _ = store.get_opt(delivery_index_key(delivery_id), _IndexEntry)
    if idx is None:
        return None
    rec, _ = store.get_opt(delivery_key(idx.asset_id, idx.version_id, delivery_id), DeliveryRecord)
    return rec


def manifest_bytes(store: ProjectStore, record: DeliveryRecord) -> bytes:
    raw = store.artifact_bytes(record.manifest_artifact_id, max_bytes=64 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != record.manifest_sha256:
        raise IntegrityError(f"integrity_mismatch: manifest of {record.delivery_id} does not match its record")
    return raw


def artifact_marker(store: ProjectStore, artifact_id: str) -> dict[str, Any] | None:
    marker, _ = store.get_opt(marker_key(artifact_id), _Marker)
    return marker.model_dump() if marker else None


class _IndexEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str
    version_id: str


class _Marker(_IndexEntry):
    delivery_id: str
