"""Descriptor + delivery preparation for the integration read API.

Legacy model3d versions get a deterministic projection of their GLB; versions that already carry a published
`descriptor` artifact are frozen as-is. Records are immutable and first-write-wins (see assetstudio_storage.delivery).
"""
from __future__ import annotations

import functools
import io
import zipfile
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from typing import Any

from assetstudio_core.delivery import (
    KNOWN_CAPABILITIES,
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
SOURCE_PROFILE = ("published_descriptor", "2")  # v1 listed only part of the verified capabilities
MAX_CHAIN = 16
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
    deliveries: list[DeliveryRecord] = field(default_factory=list)  # ready current-profile records
    reason: str | None = None
    issues: dict[str, tuple[str, str, str]] = field(default_factory=dict)  # representation -> (state, code, reason)


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


class DependencyUnavailable(Exception):
    """Transient: the closure cannot be resolved right now; nothing is frozen."""


class DependencyRejected(DependencyUnavailable):
    """Permanent for these inputs (cycle, depth): reported as unsupported_source_dependency."""


class DependencyConflict(DependencyRejected):
    pass


@dataclass
class _Pending:
    representation: str
    profile: tuple[str, str]
    preparer: dict[str, str]
    files: list[DeliveryFile]
    capabilities: list[str]
    budget: dict[str, Any]
    dependencies: list[DeliveryDependency] | None = None


@dataclass
class _Plan:
    """Everything computed without a lock; committed in one short critical section."""
    raw: bytes
    origin: Any
    profile: tuple[str, str]
    pending: list[_Pending] = field(default_factory=list)
    issues: dict[str, tuple[str, str, str]] = field(default_factory=dict)


def _expected(version: AssetVersion) -> list[str]:
    if "descriptor" not in version.artifacts:
        return [PORTABLE]
    return [r for r, role in ((PORTABLE, "model"), (SOURCE, "godot_source")) if role in version.artifacts]


def _current_profile(version: AssetVersion, representation: str) -> tuple[str, str]:
    if representation == SOURCE:
        return SOURCE_PROFILE
    return PUBLISHED_PROFILE if "descriptor" in version.artifacts else LEGACY_PROFILE


def _current(version: AssetVersion, found: list[DeliveryRecord]) -> list[DeliveryRecord]:
    return [d for d in found if (d.profile_id, d.profile_version) == _current_profile(version, d.representation)]


def _existing(store: ProjectStore, asset_id: str, version: AssetVersion, want: Collection[str]
              ) -> EnsureResult | None:
    desc = store_delivery.descriptor(store, asset_id, version.version_id)
    if desc is None:
        return None
    have = _current(version, store_delivery.deliveries(store, asset_id, version.version_id))
    if not set(want) <= {d.representation for d in have}:
        return None
    return EnsureResult("ready", desc, have)


def _source_manifest(store: ProjectStore, version: AssetVersion) -> Any:
    art = store.verify_artifact(version.artifacts["godot_source"]["artifact_id"])
    path = store.repo.blob_path(art.sha256) if hasattr(store.repo, "blob_path") else None
    src: Any = path if path is not None else io.BytesIO(store.artifact_bytes(art.id, max_bytes=MAX_GLB))
    with zipfile.ZipFile(src) as zf:
        info = zf.getinfo("source_manifest.json")
        if info.file_size > MAX_SOURCE_MANIFEST:
            raise DependencyUnavailable("source manifest too large")
        return parse_source_manifest(zf.read(info))


def source_capabilities(version: AssetVersion, declared: Callable[[], Iterable[str]]) -> list[str]:
    """Verified capabilities of a source package. The server's validator verdict is authoritative; the publisher's
    declared set (a superset of it) covers versions published before the verdict was stored."""
    detected = (version.validation.get("source") or {}).get("detected_capabilities")
    found = {"godot_text_scene_v1"} | set(detected if detected is not None else declared())
    return [c for c in KNOWN_CAPABILITIES if c in found]


def _source_dependencies(studio: Studio, ctx: ProjectContext, version: AssetVersion, chain: tuple[str, ...],
                         manifest: Callable[[], Any]) -> list[DeliveryDependency]:
    """Exact transitive closure for a source delivery: the keys the validated scene graph uses, plus whatever
    each dependency's own (immutable) delivery manifest already lists."""
    used = sorted(((version.validation.get("source") or {}).get("asset_dependencies")) or [])
    if not used:
        return []
    declared = manifest().asset_dependencies
    out: dict[str, DeliveryDependency] = {}
    for key in used:
        if key not in declared:
            raise DependencyUnavailable(f"{key[:12]} not declared in the source manifest")
        _add_dependency(studio, declared[key], out, chain)
    return [out[k] for k in sorted(out)]


def _merge(out: dict[str, DeliveryDependency], dep: DeliveryDependency) -> None:
    have = out.get(dep.asset_key)
    if have is None:
        out[dep.asset_key] = dep
    elif (have.representation, have.delivery_id, have.manifest_sha256) != (
            dep.representation, dep.delivery_id, dep.manifest_sha256):
        raise DependencyConflict(f"conflicting requirements for {dep.asset_key[:12]}")


def _pinned(dctx: ProjectContext, ref: AssetRef, dep: SourceAssetDependency
            ) -> tuple[tuple[DescriptorRecord, bytes], DeliveryRecord] | None:
    """A pinned delivery id stays exact even when it belongs to an older profile."""
    if dep.delivery_id is None:
        return None
    desc = store_delivery.descriptor(dctx.store, ref.asset_id, ref.version_id)
    found = store_delivery.deliveries(dctx.store, ref.asset_id, ref.version_id) if desc else []
    rec = next((d for d in found if d.delivery_id == dep.delivery_id and d.representation == dep.representation),
               None)
    return (desc, rec) if desc is not None and rec is not None else None


def _dependency_record(studio: Studio, dctx: ProjectContext, ref: AssetRef, dep: SourceAssetDependency,
                       chain: tuple[str, ...]) -> tuple[DescriptorRecord, DeliveryRecord]:
    stored = _pinned(dctx, ref, dep)
    if stored is not None:
        return stored[0][0], stored[1]
    result = ensure_version(studio, dctx, ref.server_id, ref.asset_id, ref.version_id,
                            representations=[dep.representation], _chain=chain)
    where = f"{ref.asset_id}/{ref.version_id}"
    if result.state != "ready" or result.descriptor is None:
        raise DependencyUnavailable(f"{where} is {result.state}")
    wanted = [d for d in result.deliveries if d.representation == dep.representation
              and (dep.delivery_id is None or d.delivery_id == dep.delivery_id)]
    if len(wanted) == 1:
        return result.descriptor[0], wanted[0]
    issue = result.issues.get(dep.representation)
    if issue is not None:
        exc = DependencyUnavailable if issue[0] == "temporarily_unavailable" else DependencyRejected
        raise exc(f"{where} {dep.representation}: {issue[2]}")
    raise DependencyUnavailable(f"{where} has no {dep.representation} delivery")


def _add_dependency(studio: Studio, dep: SourceAssetDependency, out: dict[str, DeliveryDependency],
                    chain: tuple[str, ...]) -> None:
    ref = dep.asset_ref
    try:
        dctx = studio.registry.get(ref.library_id)
    except ApiError:
        raise DependencyUnavailable(f"library {ref.library_id} unavailable") from None
    desc, rec = _dependency_record(studio, dctx, ref, dep, chain)
    if desc.descriptor_sha256 != dep.descriptor_sha256:
        raise DependencyUnavailable(f"{ref.asset_id}/{ref.version_id} descriptor hash differs")
    _merge(out, DeliveryDependency(
        asset_key=ref.key(), asset_ref=ref, descriptor_sha256=dep.descriptor_sha256,
        representation=rec.representation, delivery_id=rec.delivery_id, manifest_sha256=rec.manifest_sha256))
    for nested in parse_manifest(store_delivery.manifest_bytes(dctx.store, rec)).dependencies:
        _merge(out, nested)


def _files_for(store: ProjectStore, version: AssetVersion, role: str, path: str, media: str) -> list[DeliveryFile]:
    ref = version.artifacts[role]
    art = store.verify_artifact(ref["artifact_id"])
    return [DeliveryFile(path=path, sha256=art.sha256, size=art.size, media_type=media, artifact_id=art.id)]


def _store_delivery(ctx: ProjectContext, ref: AssetRef, descriptor_sha: str, p: _Pending) -> bool:
    delivery_id = derived_id("dlv", ctx.id, ref.asset_id, ref.version_id, p.representation, *p.profile)
    manifest: DeliveryManifestV1 = build_manifest(
        delivery_id, ref, descriptor_sha, p.representation, p.profile, p.preparer, p.files, p.files[0].path,
        p.capabilities, p.dependencies or [])
    fields = {"delivery_id": delivery_id, "asset_id": ref.asset_id, "version_id": ref.version_id,
              "representation": p.representation, "profile_id": p.profile[0], "profile_version": p.profile[1],
              "total_bytes": sum(f.size for f in p.files), "budget": p.budget}
    _, created = store_delivery.put_delivery(ctx.store, fields, manifest_bytes(manifest),
                                             [f.artifact_id for f in p.files])
    return created


def _early(reps: Iterable[str], reason: str) -> EnsureResult:
    return EnsureResult("unsupported", reason=reason, issues={r: ("unsupported", "unsupported_representation", reason)
                                                              for r in reps})


def _legacy(ctx: ProjectContext, ref: AssetRef, version: AssetVersion, limits: dict[str, Any]
            ) -> _Plan | EnsureResult:
    store = ctx.store
    model = version.artifacts.get("model")
    if model is None:
        return _early([PORTABLE], "version has no model artifact")
    data = store.artifact_bytes(model["artifact_id"], max_bytes=MAX_GLB)
    try:
        info = inspect_static_glb(data)
        doc, _ = glb_json(data)
        descriptor = build_descriptor(ref, version, doc, info["bounds"])
    except (TransformRejected, GlbRejected, ValidationError, ValueError) as e:
        return _early([PORTABLE], str(e)[:300])
    files = _files_for(store, version, "model", "model.glb", "model/gltf-binary")
    plan = _Plan(descriptor_bytes(descriptor), "projection", LEGACY_PROFILE)
    plan.pending.append(_Pending(PORTABLE, LEGACY_PROFILE, PREPARER, files, glb_capabilities(doc),
                                 glb_budget(data, limits)))
    return plan


def _plan_portable(store: ProjectStore, version: AssetVersion, plan: _Plan, limits: dict[str, Any]) -> None:
    data = store.artifact_bytes(version.artifacts["model"]["artifact_id"], max_bytes=MAX_GLB)
    files = _files_for(store, version, "model", "model.glb", "model/gltf-binary")
    try:
        caps = glb_capabilities(glb_json(data)[0])
    except (GlbRejected, ValueError) as e:
        plan.issues[PORTABLE] = ("unsupported", "unsupported_representation", str(e)[:300])
        return
    plan.pending.append(_Pending(PORTABLE, PUBLISHED_PROFILE, PUBLISHED_PREPARER, files, caps,
                                 glb_budget(data, limits)))


def _plan_source(studio: Studio, ctx: ProjectContext, version: AssetVersion, plan: _Plan,
                 chain: tuple[str, ...]) -> None:
    manifest = functools.cache(functools.partial(_source_manifest, ctx.store, version))
    try:
        deps = _source_dependencies(studio, ctx, version, chain, manifest)
    except (DependencyUnavailable, LookupFailed) as e:
        # Never freeze a source delivery without its exact closure (first write wins forever).
        reason = f"source dependency: {e}"[:300]
        plan.issues[SOURCE] = (("unsupported", "unsupported_source_dependency", reason)
                               if isinstance(e, DependencyRejected)
                               else ("temporarily_unavailable", "temporarily_unavailable", reason))
        return
    files = _files_for(ctx.store, version, "godot_source", "source.zip", "application/zip")
    caps = source_capabilities(version, lambda: manifest().capabilities)
    budget = {"within_ipad_budget": None, "exceeded": [], "warnings": ["source package not measured"]}
    plan.pending.append(_Pending(SOURCE, SOURCE_PROFILE, PUBLISHED_PREPARER, files, caps, budget, deps))


def _published(studio: Studio, ctx: ProjectContext, ref: AssetRef, version: AssetVersion, limits: dict[str, Any],
               missing: list[str], chain: tuple[str, ...]) -> _Plan | EnsureResult:
    raw = ctx.store.artifact_bytes(version.artifacts["descriptor"]["artifact_id"], max_bytes=16 * 1024 * 1024)
    try:
        parsed = parse_descriptor(raw)
    except ValueError as e:
        return _early(missing, f"published descriptor invalid: {str(e)[:250]}")
    if parsed.asset_ref != ref:
        return _early(missing, "published descriptor names a different asset reference")
    plan = _Plan(raw, "published", PUBLISHED_PROFILE)
    if PORTABLE in missing:
        _plan_portable(ctx.store, version, plan, limits)
    if SOURCE in missing:
        _plan_source(studio, ctx, version, plan, chain)
    return plan


def _commit(ctx: ProjectContext, ref: AssetRef, plan: _Plan) -> bool:
    """Short critical section: immutable create-if-absent writes only, no cross-library calls."""
    with ctx.store.lock:
        rec = store_delivery.freeze_descriptor(ctx.store, ref.asset_id, ref.version_id, plan.raw, plan.origin,
                                               *plan.profile)
        created = False
        for pending in plan.pending:
            created |= _store_delivery(ctx, ref, rec.descriptor_sha256, pending)
    return created


def _result(store: ProjectStore, asset_id: str, version: AssetVersion, want: list[str],
            issues: dict[str, tuple[str, str, str]]) -> EnsureResult:
    desc = store_delivery.descriptor(store, asset_id, version.version_id)
    have = _current(version, store_delivery.deliveries(store, asset_id, version.version_id))
    if desc is not None and {d.representation for d in have} & set(want):
        return EnsureResult("ready", desc, have, issues=issues)
    state, _, reason = next(iter(issues.values()), ("temporarily_unavailable", "", "delivery not stored"))
    return EnsureResult(state, desc, have, reason, issues)


def ensure_version(studio: Studio, ctx: ProjectContext, server_id: str, asset_id: str, version_id: str,
                   limits: dict[str, Any] | None = None, *, representations: Collection[str] | None = None,
                   _chain: tuple[str, ...] = ()) -> EnsureResult:
    """Prepare (once) and return a version's descriptor + deliveries. Pure read when already prepared.

    Computation (including other libraries' dependency preparation) runs without any project lock, so two
    libraries preparing versions that depend on each other cannot deadlock."""
    _, version = load_version(ctx.store, asset_id, version_id)
    expected = _expected(version)
    want = expected if representations is None else [r for r in expected if r in set(representations)]
    if not want:
        return EnsureResult("unsupported", reason="no requested representation available")
    done = _existing(ctx.store, asset_id, version, want)
    if done is not None:
        return done
    if ctx.read_only:
        return EnsureResult("temporarily_unavailable", reason="library is open read-only; cannot prepare deliveries")
    key = f"{ctx.id}/{asset_id}/{version_id}"
    if key in _chain:
        raise DependencyRejected("dependency cycle")
    if len(_chain) >= MAX_CHAIN:
        raise DependencyRejected("dependency chain too deep")
    ref = AssetRef(server_id=server_id, library_id=ctx.id, asset_id=asset_id, version_id=version_id)
    have = {d.representation for d in _current(version, store_delivery.deliveries(ctx.store, asset_id, version_id))}
    missing = [r for r in want if r not in have]
    plan = (_published(studio, ctx, ref, version, limits or {}, missing, _chain + (key,))
            if "descriptor" in version.artifacts else _legacy(ctx, ref, version, limits or {}))
    if isinstance(plan, EnsureResult):
        return plan
    created = _commit(ctx, ref, plan) if plan.pending else False
    if created:
        studio.events.publish("library", project_id=ctx.id, asset_id=asset_id, change="delivery_ready")
    return _result(ctx.store, asset_id, version, want, plan.issues)


def summary(rec: DeliveryRecord) -> dict[str, Any]:
    return {"delivery_id": rec.delivery_id, "representation": rec.representation, "profile_id": rec.profile_id,
            "profile_version": rec.profile_version, "manifest_sha256": rec.manifest_sha256,
            "total_bytes": rec.total_bytes, "budget": rec.budget}
