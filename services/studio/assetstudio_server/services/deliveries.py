"""Descriptor + delivery preparation for the integration read API.

Legacy model3d versions get a deterministic projection of their GLB; versions that already carry a published
`descriptor` artifact are frozen as-is. Records are immutable and first-write-wins (see assetstudio_storage.delivery).
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from typing import Any

from assetstudio_core.delivery import (
    AssetRef,
    DeliveryDependency,
    DeliveryFile,
    DeliveryManifestV1,
    descriptor_bytes,
    manifest_bytes,
    parse_descriptor,
    parse_manifest,
)
from assetstudio_core.domain import AssetManifest, AssetVersion
from assetstudio_core.ids import derived_id
from assetstudio_core.source_manifest import SourceAssetDependency, parse_source_manifest
from assetstudio_processing.glb import GlbRejected
from assetstudio_processing.glb_budget import glb_budget, glb_json
from assetstudio_processing.transforms import TransformRejected, inspect_static_glb
from assetstudio_storage import delivery as store_delivery
from assetstudio_storage.delivery import DeliveryRecord, DescriptorRecord
from assetstudio_storage.project import ProjectStore, manifest_key, version_key
from assetstudio_storage.repo import NotFound
from pydantic import ValidationError

from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from .delivery_projection import LEGACY_PROFILE, PREPARER, build_descriptor, build_manifest, glb_capabilities

MAX_GLB = 512 * 1024 * 1024
MAX_SOURCE_MANIFEST = 8 * 1024 * 1024
PUBLISHED_PROFILE = ("published_descriptor", "1")
PUBLISHED_PREPARER = {"name": "assetstudio.published_descriptor", "version": "1"}
PORTABLE, SOURCE = "portable_glb_v1", "godot_static_source_v1"


class LookupFailed(Exception):
    """`code`: asset_not_found | version_unavailable (only model3d assets are visible to the integration API)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class EnsureResult:
    state: str  # ready | unsupported | temporarily_unavailable
    descriptor: tuple[DescriptorRecord, bytes] | None = None
    deliveries: list[DeliveryRecord] = field(default_factory=list)
    reason: str | None = None


def load_version(store: ProjectStore, asset_id: str, version_id: str) -> tuple[AssetManifest, AssetVersion]:
    try:
        manifest = store.get(manifest_key(asset_id), AssetManifest)[0]
    except NotFound:
        raise LookupFailed("asset_not_found") from None
    if manifest.kind.value != "model3d":
        raise LookupFailed("asset_not_found")
    if manifest.version(version_id) is None:
        raise LookupFailed("version_unavailable")
    try:
        return manifest, store.get(version_key(asset_id, version_id), AssetVersion)[0]
    except NotFound:
        raise LookupFailed("version_unavailable") from None


def _expected(version: AssetVersion) -> list[str]:
    if "descriptor" not in version.artifacts:
        return [PORTABLE]
    return [r for r, role in ((PORTABLE, "model"), (SOURCE, "godot_source")) if role in version.artifacts]


def _existing(store: ProjectStore, asset_id: str, version: AssetVersion) -> EnsureResult | None:
    desc = store_delivery.descriptor(store, asset_id, version.version_id)
    if desc is None:
        return None
    found = store_delivery.deliveries(store, asset_id, version.version_id)
    if not {d.representation for d in found} >= set(_expected(version)):
        return None
    return EnsureResult("ready", desc, found)


class DependencyUnavailable(Exception):
    pass


def _source_manifest(store: ProjectStore, version: AssetVersion) -> Any:
    art = store.verify_artifact(version.artifacts["godot_source"]["artifact_id"])
    path = store.repo.blob_path(art.sha256) if hasattr(store.repo, "blob_path") else None
    src: Any = path if path is not None else io.BytesIO(store.artifact_bytes(art.id, max_bytes=MAX_GLB))
    with zipfile.ZipFile(src) as zf:
        info = zf.getinfo("source_manifest.json")
        if info.file_size > MAX_SOURCE_MANIFEST:
            raise DependencyUnavailable("source manifest too large")
        return parse_source_manifest(zf.read(info))


def _source_dependencies(studio: Studio, ctx: ProjectContext, version: AssetVersion) -> list[DeliveryDependency]:
    """Exact transitive closure for a source delivery: the keys the validated scene graph uses, plus whatever
    each dependency's own (immutable) delivery manifest already lists."""
    used = sorted(((version.validation.get("source") or {}).get("asset_dependencies")) or [])
    if not used:
        return []
    manifest = _source_manifest(ctx.store, version)
    out: dict[str, DeliveryDependency] = {}
    for key in used:
        if key not in manifest.asset_dependencies:
            raise DependencyUnavailable(f"{key[:12]} not declared in the source manifest")
        _add_dependency(studio, manifest.asset_dependencies[key], out)
    return [out[k] for k in sorted(out)]


def _add_dependency(studio: Studio, dep: SourceAssetDependency, out: dict[str, DeliveryDependency]) -> None:
    ref = dep.asset_ref
    try:
        dctx = studio.registry.get(ref.library_id)
    except ApiError:
        raise DependencyUnavailable(f"library {ref.library_id} unavailable") from None
    result = ensure_version(studio, dctx, ref.server_id, ref.asset_id, ref.version_id)
    if result.state != "ready" or result.descriptor is None:
        raise DependencyUnavailable(f"{ref.asset_id}/{ref.version_id} is {result.state}")
    if result.descriptor[0].descriptor_sha256 != dep.descriptor_sha256:
        raise DependencyUnavailable(f"{ref.asset_id}/{ref.version_id} descriptor hash differs")
    wanted = [d for d in result.deliveries if d.representation == dep.representation
              and (dep.delivery_id is None or d.delivery_id == dep.delivery_id)]
    rec = wanted[0] if len(wanted) == 1 else None
    if rec is None:
        raise DependencyUnavailable(f"{ref.asset_id}/{ref.version_id} has no {dep.representation} delivery")
    out[ref.key()] = DeliveryDependency(
        asset_key=ref.key(), asset_ref=ref, descriptor_sha256=dep.descriptor_sha256,
        representation=rec.representation, delivery_id=rec.delivery_id, manifest_sha256=rec.manifest_sha256)
    for nested in parse_manifest(store_delivery.manifest_bytes(dctx.store, rec)).dependencies:
        out.setdefault(nested.asset_key, nested)


def _files_for(store: ProjectStore, version: AssetVersion, role: str, path: str, media: str) -> list[DeliveryFile]:
    ref = version.artifacts[role]
    art = store.verify_artifact(ref["artifact_id"])
    return [DeliveryFile(path=path, sha256=art.sha256, size=art.size, media_type=media, artifact_id=art.id)]


def _store_delivery(ctx: ProjectContext, ref: AssetRef, descriptor_sha: str, representation: str,
                    profile: tuple[str, str], preparer: dict[str, str], files: list[DeliveryFile],
                    capabilities: list[str], budget: dict[str, Any],
                    dependencies: list[DeliveryDependency] | None = None) -> bool:
    delivery_id = derived_id("dlv", ctx.id, ref.asset_id, ref.version_id, representation, *profile)
    manifest: DeliveryManifestV1 = build_manifest(delivery_id, ref, descriptor_sha, representation, profile, preparer,
                                                  files, files[0].path, capabilities, dependencies or [])
    fields = {"delivery_id": delivery_id, "asset_id": ref.asset_id, "version_id": ref.version_id,
              "representation": representation, "profile_id": profile[0], "profile_version": profile[1],
              "total_bytes": sum(f.size for f in files), "budget": budget}
    _, created = store_delivery.put_delivery(ctx.store, fields, manifest_bytes(manifest),
                                             [f.artifact_id for f in files])
    return created


def _legacy(studio: Studio, ctx: ProjectContext, ref: AssetRef, version: AssetVersion, limits: dict[str, Any]
            ) -> tuple[bool, EnsureResult | None]:
    """(created_any, early_result). Early result is set only for an unsupported model."""
    store = ctx.store
    model = version.artifacts.get("model")
    if model is None:
        return False, EnsureResult("unsupported", reason="version has no model artifact")
    data = store.artifact_bytes(model["artifact_id"], max_bytes=MAX_GLB)
    try:
        info = inspect_static_glb(data)
        doc, _ = glb_json(data)
        descriptor = build_descriptor(ref, version, doc, info["bounds"])
    except (TransformRejected, GlbRejected, ValidationError, ValueError) as e:
        return False, EnsureResult("unsupported", reason=str(e)[:300])
    raw = descriptor_bytes(descriptor)
    rec = store_delivery.freeze_descriptor(store, ref.asset_id, ref.version_id, raw, "projection", *LEGACY_PROFILE)
    files = _files_for(store, version, "model", "model.glb", "model/gltf-binary")
    created = _store_delivery(ctx, ref, rec.descriptor_sha256, PORTABLE, LEGACY_PROFILE, PREPARER, files,
                              glb_capabilities(doc), glb_budget(data, limits))
    return created, None


def _published(studio: Studio, ctx: ProjectContext, ref: AssetRef, version: AssetVersion, limits: dict[str, Any]
               ) -> tuple[bool, EnsureResult | None]:
    store = ctx.store
    raw = store.artifact_bytes(version.artifacts["descriptor"]["artifact_id"], max_bytes=16 * 1024 * 1024)
    try:
        parsed = parse_descriptor(raw)
    except ValueError as e:
        return False, EnsureResult("unsupported", reason=f"published descriptor invalid: {str(e)[:250]}")
    if parsed.asset_ref != ref:
        return False, EnsureResult("unsupported", reason="published descriptor names a different asset reference")
    rec = store_delivery.freeze_descriptor(store, ref.asset_id, ref.version_id, raw, "published", *PUBLISHED_PROFILE)
    created = False
    if "model" in version.artifacts:
        data = store.artifact_bytes(version.artifacts["model"]["artifact_id"], max_bytes=MAX_GLB)
        files = _files_for(store, version, "model", "model.glb", "model/gltf-binary")
        try:
            caps = glb_capabilities(glb_json(data)[0])
        except (GlbRejected, ValueError) as e:
            return False, EnsureResult("unsupported", reason=str(e)[:300])
        created |= _store_delivery(ctx, ref, rec.descriptor_sha256, PORTABLE, PUBLISHED_PROFILE, PUBLISHED_PREPARER,
                                   files, caps, glb_budget(data, limits))
    if "godot_source" in version.artifacts:
        try:
            deps = _source_dependencies(studio, ctx, version)
        except (DependencyUnavailable, LookupFailed) as e:
            # Never freeze a source delivery without its exact closure (first write wins forever).
            return created, EnsureResult("temporarily_unavailable", reason=f"source dependency: {e}"[:300])
        files = _files_for(store, version, "godot_source", "source.zip", "application/zip")
        caps = ["godot_text_scene_v1"] + (["static_collision"] if parsed.collision else [])
        budget = {"within_ipad_budget": None, "exceeded": [], "warnings": ["source package not measured"]}
        created |= _store_delivery(ctx, ref, rec.descriptor_sha256, SOURCE, PUBLISHED_PROFILE, PUBLISHED_PREPARER,
                                   files, caps, budget, deps)
    return created, None


def ensure_version(studio: Studio, ctx: ProjectContext, server_id: str, asset_id: str, version_id: str,
                   limits: dict[str, Any] | None = None) -> EnsureResult:
    """Prepare (once) and return a version's descriptor + deliveries. Pure read when already prepared."""
    _, version = load_version(ctx.store, asset_id, version_id)
    done = _existing(ctx.store, asset_id, version)
    if done is not None:
        return done
    if ctx.read_only:
        return EnsureResult("temporarily_unavailable", reason="library is open read-only; cannot prepare deliveries")
    ref = AssetRef(server_id=server_id, library_id=ctx.id, asset_id=asset_id, version_id=version_id)
    with ctx.store.lock:
        done = _existing(ctx.store, asset_id, version)
        if done is not None:
            return done
        build = _published if "descriptor" in version.artifacts else _legacy
        created, early = build(studio, ctx, ref, version, limits or {})
        if early is not None:
            return early
        result = _existing(ctx.store, asset_id, version)
    if created:
        studio.events.publish("library", project_id=ctx.id, asset_id=asset_id, change="delivery_ready")
    return result or EnsureResult("temporarily_unavailable", reason="delivery not stored")


def summary(rec: DeliveryRecord) -> dict[str, Any]:
    return {"delivery_id": rec.delivery_id, "representation": rec.representation, "profile_id": rec.profile_id,
            "profile_version": rec.profile_version, "manifest_sha256": rec.manifest_sha256,
            "total_bytes": rec.total_bytes, "budget": rec.budget}
