"""Explicit, atomic static-source publication for the integration API: preview (validate + expiring receipt, no
library mutation) then commit (one immutable version: portable GLB + source package + descriptor + report).

Replay safety: every identity derives from the idempotency key (`op_id`), the publish receipt in the library is the
durable commit point, and the staging directory is only removed after the journal records the response. The key is
bound to the whole semantic request plus the publisher's immutable credential id (`_binding`): an intent record is
created before any effect and the digest is stored with the publication, so crash recovery and replays by a different
request or credential conflict. Commits of one op are serialized; previews are owned by the credential id.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from assetstudio_core.canonical_v1 import canonical_bytes
from assetstudio_core.delivery import AssetDescriptorV1, AssetRef, MaterialSlot, SourceSurface, descriptor_bytes
from assetstudio_core.domain import Artifact, AssetManifest, AssetVersion
from assetstudio_core.ids import derived_id, is_id, new_id
from assetstudio_core.kinds import Kind, Origin
from assetstudio_core.naming import slug
from assetstudio_core.publication_draft import draft_bytes, parse_draft
from assetstudio_storage.project import artifact_key, manifest_key, version_key
from assetstudio_storage.publication import NewAsset, PublishRequest, StalePointer, name_key, publish, receipt_key
from assetstudio_storage.repo import Conflict, NotFound, StorageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..journal import IdempotencyConflict
from ..registry import ProjectContext
from ..secure_files import write_private
from ..studio import Studio
from . import deliveries as svc
from . import source_publication_checks as chk
from .principals import Principal, ServiceError
from .records import cmd_payload

log = logging.getLogger("assetstudio.integration")
EXPIRY, ORPHAN_AGE, SWEEP_BATCH = timedelta(hours=24), timedelta(hours=1), 50
REQUIRED_PARTS, OPTIONAL_PARTS = ("portable", "descriptor"), ("source", "report", "thumbnail")
ACTION = "integration_publish"
_OP_LOCKS: dict[str, list[Any]] = {}  # op_id -> [lock, users]; dropped when unused
_OP_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class StagedPart:
    path: Path
    sha256: str
    size: int


class CommitPublication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_id: str
    target_asset_id: str | None = None
    expected_current_version: str | None = None
    package_sha256: str | None = None
    portable_sha256: str
    descriptor_draft_sha256: str
    name: str = Field(min_length=1, max_length=200)
    category_id: str | None = None
    tags: list[str] = Field(default=[], max_length=50)
    licence: str = Field(default="unknown", max_length=200)
    source_uri: str | None = Field(default=None, max_length=1000)
    credit: str | None = Field(default=None, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=100)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# --- staging ---------------------------------------------------------------------------------------------------------
def staging_root(settings: Any) -> Path:
    return settings.instance_dir / "staging" / "integration"


def new_preview(settings: Any) -> tuple[str, Path]:
    """(preview id, empty parts directory) for the route to stream the upload into."""
    preview_id = new_id("ipv")
    parts = staging_root(settings) / preview_id / "parts"
    parts.mkdir(parents=True)
    return preview_id, parts


def discard(settings: Any, preview_id: str) -> None:
    if is_id(preview_id, "ipv"):
        shutil.rmtree(staging_root(settings) / preview_id, ignore_errors=True)


def _read_receipt(directory: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((directory / "receipt.json").read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _expired(directory: Path, now: datetime) -> bool:
    receipt = _read_receipt(directory)
    try:
        if receipt is not None:
            return _parse(receipt["expires_at"]) < now
        return datetime.fromtimestamp(directory.stat().st_mtime, UTC) < now - ORPHAN_AGE
    except (KeyError, ValueError, OSError):
        return False


def sweep_expired(settings: Any, now: datetime | None = None, active: frozenset[str] = frozenset()) -> int:
    """Remove up to SWEEP_BATCH expired preview directories (receipt expiry, or receipt-less and older than 1 h);
    directories named in `active` are in use and never removed."""
    root, now, removed = staging_root(settings), now or _now(), 0
    if not root.is_dir():
        return 0
    for d in sorted(root.iterdir()):
        if removed >= SWEEP_BATCH:
            break
        if d.is_dir() and d.name.startswith("ipv_") and d.name not in active and _expired(d, now):
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


# --- preview ---------------------------------------------------------------------------------------------------------
def preview(studio: Studio, ctx: ProjectContext, who: Principal, server_id: str, parts: dict[str, StagedPart],
            capabilities: dict[str, Any], active: frozenset[str] = frozenset()) -> dict[str, Any]:
    ctx.require_writable()
    directory = next(iter(parts.values())).path.parent.parent
    try:
        sweep_expired(studio.settings, active=active)
        receipt = _build_preview(studio, ctx, who, server_id, parts, capabilities, directory)
        write_private(directory / "receipt.json", receipt)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return receipt


def _require_parts(parts: dict[str, StagedPart]) -> None:
    missing = [n for n in REQUIRED_PARTS if n not in parts]
    if ("source" in parts) != ("report" in parts):
        missing.append("report" if "source" in parts else "source")
    if missing:
        raise ServiceError("invalid_request", "missing or unexpected multipart parts",
                               details={"parts": sorted(set(missing))})


def _build_preview(studio: Studio, ctx: ProjectContext, who: Principal, server_id: str, parts: dict[str, StagedPart],
                   capabilities: dict[str, Any], directory: Path) -> dict[str, Any]:
    _require_parts(parts)
    limits = capabilities["limits"]
    glb = chk.check_glb(parts["portable"].path.read_bytes(), limits)
    draft = chk.parse_draft_part(parts["descriptor"].path.read_bytes())
    chk.check_surfaces(draft, glb.doc)
    chk.check_anchor(draft, glb)
    if "thumbnail" in parts:
        chk.check_thumbnail(parts["thumbnail"].path.read_bytes())
    warnings = set(glb.warnings) | set(draft.preview_warnings)
    source = None
    if "source" in parts:
        resolver = chk.dependency_resolver(studio, ctx, who, server_id, limits)
        source, extra = _check_source(parts, draft, capabilities, resolver, directory)
        warnings |= extra
    else:
        chk.check_draft_without_source(draft)
    raw = draft_bytes(draft)
    now = _now()
    return {"preview_id": directory.name, "library_id": ctx.id, "token_name": who.token_name,
            "credential_id": who.credential_id,
            "created_at": _iso(now), "expires_at": _iso(now + EXPIRY),
            "parts": {n: {"sha256": p.sha256, "size": p.size} for n, p in sorted(parts.items())},
            "package_sha256": parts["source"].sha256 if "source" in parts else None,
            "portable_sha256": parts["portable"].sha256, "descriptor_draft_sha256": hashlib.sha256(raw).hexdigest(),
            "descriptor_draft": raw.decode(), "bounds": {"min": glb.bounds_min, "max": glb.bounds_max},
            "budget": glb.budget, "source": source,
            "report_sha256": parts["report"].sha256 if "report" in parts else None,
            "warnings": sorted(warnings)[:64]}


def _check_source(parts: dict[str, StagedPart], draft: Any, capabilities: dict[str, Any], resolver: Any,
                  directory: Path) -> tuple[dict[str, Any], set[str]]:
    work = directory / "source"
    facts = chk.check_source(parts["source"].path, work, capabilities, resolver)
    report = chk.parse_report(parts["report"].path.read_bytes())
    surfaces = chk.check_agreement(draft, facts, report)
    shutil.rmtree(work, ignore_errors=True)  # only the validated verdict is kept, not the expanded tree
    summary = {"manifest_sha256": facts.report.manifest_sha256,
               "detected_capabilities": facts.report.detected_capabilities,
               "dependency_closure": facts.report.dependency_closure,
               "asset_dependencies": facts.report.asset_dependencies, "warnings": facts.warnings,
               "source_godot_version": facts.manifest.source_godot_version, "source_surfaces": surfaces,
               "evidence": facts.evidence}
    return summary, set(facts.warnings) | set(chk.report_warnings(report))


# --- commit ----------------------------------------------------------------------------------------------------------
def _op_id(ctx: ProjectContext, key: str) -> str:
    return derived_id("op", ctx.id, "integration", key)


def _publish_receipt(ctx: ProjectContext, op_id: str) -> dict[str, Any] | None:
    try:
        return json.loads(ctx.store.repo.read_object(receipt_key(op_id)).data)
    except NotFound:
        return None


def _binding(req: CommitPublication, who: Principal) -> str:
    """Digest of the whole semantic request (not the retry-only fields) and the publisher's credential."""
    body = {k: v for k, v in cmd_payload(req).items() if k not in ("idempotency_key", "job_id")}
    return hashlib.sha256(canonical_bytes({**body, "credential_id": who.credential_id})).hexdigest()


@contextmanager
def _op_lock(op_id: str) -> Iterator[None]:
    with _OP_LOCKS_GUARD:
        entry = _OP_LOCKS.setdefault(op_id, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _OP_LOCKS_GUARD:
            entry[1] -= 1
            if entry[1] == 0:
                _OP_LOCKS.pop(op_id, None)


def _intent_key(op_id: str) -> str:
    return f"integration_ops/{op_id}.json"


def _record_intent(ctx: ProjectContext, who: Principal, req: CommitPublication, op_id: str, digest: str) -> None:
    """Create-if-absent before any effect: a crashed attempt keeps the key bound to its request and credential."""
    record = {"op_id": op_id, "request_sha256": digest, "credential_id": who.credential_id,
              "preview_id": req.preview_id}
    try:
        ctx.store.create(_intent_key(op_id), record)
    except Conflict:
        if _intent_digest(ctx, op_id) != digest:
            raise _key_reused() from None


def _intent_digest(ctx: ProjectContext, op_id: str) -> str | None:
    try:
        return json.loads(ctx.store.repo.read_object(_intent_key(op_id)).data).get("request_sha256")
    except NotFound:
        return None


def _key_reused() -> ServiceError:
    return ServiceError("idempotency_conflict", "idempotency key reused with a different request")


def operation(ctx: ProjectContext, key: str) -> dict[str, Any]:
    op_id = _op_id(ctx, key)
    done = _publish_receipt(ctx, op_id)
    if done is None:
        return {"idempotency_key": key, "op_id": op_id, "state": "unknown"}
    return {"idempotency_key": key, "op_id": op_id, "state": "committed", "asset_id": done["asset_id"],
            "version_id": done["version_id"], "display_version": done["display_version"]}


def commit(studio: Studio, ctx: ProjectContext, who: Principal, server_id: str, req: CommitPublication,
           limits: dict[str, Any] | None = None) -> dict[str, Any]:
    ctx.require_writable()
    if (req.target_asset_id is None) != (req.expected_current_version is None):
        raise ServiceError("invalid_request",
                               "target_asset_id and expected_current_version are given together or not at all")
    if not is_id(req.preview_id, "ipv"):
        raise ServiceError("invalid_request", "invalid preview id")
    op_id = _op_id(ctx, req.idempotency_key)
    with _op_lock(op_id):
        return _commit_locked(studio, ctx, who, server_id, req, op_id, limits)


def _commit_locked(studio: Studio, ctx: ProjectContext, who: Principal, server_id: str, req: CommitPublication,
                   op_id: str, limits: dict[str, Any] | None) -> dict[str, Any]:
    payload = cmd_payload(req)
    try:
        prior = studio.journal.command_result(ctx.id, ACTION, req.idempotency_key, payload)
    except IdempotencyConflict:
        raise _key_reused() from None
    if prior is not None:
        return _replay(studio, ctx, who, server_id, req, prior, limits)
    done = _publish_receipt(ctx, op_id)
    if done is None:
        done = _publish_staged(studio, ctx, who, server_id, req, op_id)
    else:
        _check_same_request(ctx, done, req, who)
    return _finalize(studio, ctx, server_id, req, payload, done, limits)


def _replay(studio: Studio, ctx: ProjectContext, who: Principal, server_id: str, req: CommitPublication,
            prior: dict[str, Any], limits: dict[str, Any] | None) -> dict[str, Any]:
    done = _publish_receipt(ctx, _op_id(ctx, req.idempotency_key))
    if done is not None:
        _check_same_request(ctx, done, req, who)
    discard(studio.settings, req.preview_id)
    if prior["deliveries"]:
        return prior
    ref = prior["asset_ref"]  # deliveries failed to prepare at commit time: retry now (not re-recorded)
    return {**prior, "deliveries": _deliveries(studio, ctx, server_id, ref["asset_id"], ref["version_id"], limits)}


def _check_same_request(ctx: ProjectContext, done: dict[str, Any], req: CommitPublication, who: Principal) -> None:
    version = ctx.store.get(version_key(done["asset_id"], done["version_id"]), AssetVersion)[0]
    mine = version.sources.get("integration", {})
    expected = mine.get("request_sha256")
    if expected is None:
        expected = _intent_digest(ctx, _op_id(ctx, req.idempotency_key))
    if expected is not None:
        same = expected == _binding(req, who)
    else:  # published before the binding existed
        same = mine.get("preview_id") == req.preview_id and mine.get("package_sha256") == req.package_sha256
    if not same:
        raise _key_reused()


def _load_preview(studio: Studio, ctx: ProjectContext, who: Principal, req: CommitPublication) -> dict[str, Any]:
    directory = staging_root(studio.settings) / req.preview_id
    receipt = _read_receipt(directory)
    if receipt is None or _parse(receipt["expires_at"]) < _now():
        raise ServiceError("preview_expired", "preview expired or unknown; upload again")
    if "credential_id" not in receipt:  # written before previews were bound to the credential
        raise ServiceError("preview_expired", "preview expired or unknown; upload again")
    if receipt["library_id"] != ctx.id or receipt["credential_id"] != who.credential_id:
        raise ServiceError("forbidden", "token does not grant this access")
    claimed = (req.package_sha256, req.portable_sha256, req.descriptor_draft_sha256)
    if claimed != (receipt["package_sha256"], receipt["portable_sha256"], receipt["descriptor_draft_sha256"]):
        raise ServiceError("integrity_mismatch", "request hashes differ from the preview receipt")
    for name, meta in receipt["parts"].items():
        h, size = hashlib.sha256(), 0
        with (directory / "parts" / name).open("rb") as fp:
            while chunk := fp.read(1 << 20):
                h.update(chunk)
                size += len(chunk)
        if (h.hexdigest(), size) != (meta["sha256"], meta["size"]):
            raise ServiceError("integrity_mismatch", f"staged part {name} changed since preview")
    return receipt


def _target(ctx: ProjectContext, req: CommitPublication, op_id: str) -> str:
    if req.target_asset_id is None:
        return derived_id("ast", ctx.id, op_id)
    try:
        manifest = ctx.store.get(manifest_key(req.target_asset_id), AssetManifest)[0] \
            if is_id(req.target_asset_id, "ast") else None
    except NotFound:
        manifest = None
    if manifest is None or manifest.kind != Kind.model3d:
        raise ServiceError("asset_not_found", "asset not found")
    if manifest.current_version_id != req.expected_current_version:
        raise _stale(manifest.current_version_id)
    return manifest.asset_id


def _stale(current: str | None) -> ServiceError:
    return ServiceError("stale_pointer", "the asset changed since it was read",
                            details={"current_version_id": current})


def _free_name(ctx: ProjectContext, name: str, asset_id: str) -> str:
    """First free readable id by the authoritative name records; a name this asset already holds is reused (replay)."""
    base, n = slug(name), 2
    name_id = base
    while ctx.store.repo.stat_object(name_key(name_id)) is not None:
        if json.loads(ctx.store.repo.read_object(name_key(name_id)).data).get("asset_id") == asset_id:
            break
        name_id, n = f"{base}_{n}", n + 1
    return name_id


def _publish_staged(studio: Studio, ctx: ProjectContext, who: Principal, server_id: str, req: CommitPublication,
                    op_id: str) -> dict[str, Any]:
    try:
        receipt = _load_preview(studio, ctx, who, req)
    except FileNotFoundError:
        raise ServiceError("preview_expired", "preview expired or unknown; upload again") from None
    cfg, _ = ctx.config()
    if req.category_id is not None and cfg.category(req.category_id) is None:
        raise chk.invalid(f"unknown category {req.category_id}", "unknown_category")
    parts, digest = staging_root(studio.settings) / req.preview_id / "parts", _binding(req, who)
    try:
        png = _preview_png(ctx, parts, derived_id("art", ctx.id, op_id, "preview"), "thumbnail" in receipt["parts"])
        with ctx.store.lock:
            if (done := _publish_receipt(ctx, op_id)) is not None:  # committed meanwhile: no stale-pointer check
                _check_same_request(ctx, done, req, who)
                return done
            if (bound := _intent_digest(ctx, op_id)) is not None and bound != digest:
                raise _key_reused()  # before _target: a changed retry is a conflict, never a stale pointer
            asset_id = _target(ctx, req, op_id)
            _record_intent(ctx, who, req, op_id, digest)
            version_id = derived_id("ver", asset_id, op_id)
            raw = _descriptor(ctx, who, server_id, req, receipt, asset_id, version_id)
            roles = _register(ctx, who, req, parts, op_id, raw, png, receipt)
            res = _publish_roles(ctx, who, req, receipt, op_id, asset_id, roles, digest)
    except FileNotFoundError:
        raise ServiceError("preview_expired", "preview expired or unknown; upload again") from None
    return {"asset_id": res.asset_id, "version_id": res.version_id, "display_version": res.display_version}


def _descriptor(ctx: ProjectContext, who: Principal, server_id: str, req: CommitPublication, receipt: dict[str, Any],
                asset_id: str, version_id: str) -> bytes:
    draft = parse_draft(receipt["descriptor_draft"].encode())
    source = receipt["source"] or {}
    slots = []
    for s in draft.material_slots:
        surfaces: dict[str, Any] = {"portable_glb_v1": list(s.surfaces["portable_glb_v1"])}
        if s.slot_id in source.get("source_surfaces", {}):
            surfaces["godot_static_source_v1"] = [SourceSurface.model_validate(x)
                                                  for x in source["source_surfaces"][s.slot_id]]
        slots.append(MaterialSlot(slot_id=s.slot_id, role=s.role, surfaces=surfaces))
    status = "unknown" if req.licence == "unknown" else "review"
    try:
        descriptor = AssetDescriptorV1(
            schema_version=1, asset_ref=AssetRef(server_id=server_id, library_id=ctx.id, asset_id=asset_id,
                                                 version_id=version_id),
            kind="model3d", units="m", up_axis="+Y", forward_axis="+Z", bounds_min=receipt["bounds"]["min"],
            bounds_max=receipt["bounds"]["max"], placement_anchor=draft.placement_anchor,
            footprint_radius_m=draft.footprint_radius_m, scale_range=draft.scale_range,
            height_offset_range_m=draft.height_offset_range_m, default_grounding=draft.default_grounding,
            material_slots=slots, collision=draft.collision,
            preview_warnings=sorted(set(draft.preview_warnings) | set(receipt["warnings"]))[:64],
            source_provenance={"origin": "integration", "publisher": who.actor, "preview_id": req.preview_id,
                               "source_godot_version": source.get("source_godot_version")},
            licence={"status": status, "declared": req.licence, "credit": req.credit,
                     "source_uri": req.source_uri})
    except ValidationError as e:
        raise chk.invalid("descriptor could not be built from the preview", "descriptor_invalid",
                          errors=[str(e)[:300]]) from None
    return descriptor_bytes(descriptor)


def _register(ctx: ProjectContext, who: Principal, req: CommitPublication, parts: Path, op_id: str,
              descriptor: bytes, png: bytes | None, receipt: dict[str, Any]) -> dict[str, str]:
    store = ctx.store
    src = {"integration": {"actor": who.actor, "preview_id": req.preview_id}}

    def put(role: str, content: Any, mime: str, expected: str | None = None, **kw: Any) -> str:
        return store.register_artifact(content, role, mime, source=src, expected_sha256=expected,
                                       artifact_id=derived_id("art", ctx.id, op_id, role), **kw).id

    with (parts / "portable").open("rb") as fp:
        roles = {"model": put("model", fp, "model/gltf-binary", receipt["portable_sha256"])}
    if png is not None:
        roles["preview"] = put("preview", png, "image/png", lineage=[roles["model"]])
    roles["descriptor"] = put("descriptor", descriptor, "application/json")
    if "source" in receipt["parts"]:
        with (parts / "source").open("rb") as fp:
            roles["godot_source"] = put("godot_source", fp, "application/zip", receipt["package_sha256"])
        report = chk.parse_report((parts / "report").read_bytes())
        roles["conversion_report"] = put("conversion_report", canonical_bytes(report.model_dump(mode="json")),
                                         "application/json")
    return roles


def _preview_png(ctx: ProjectContext, parts: Path, art_id: str, has_thumbnail: bool) -> bytes | None:
    """The uploaded thumbnail, else a CPU render. A render is a derivative: failure never blocks publication, and a
    preview registered by an earlier attempt is reused so a retry cannot conflict with a re-render."""
    if has_thumbnail:
        return (parts / "thumbnail").read_bytes()
    if ctx.store.get_opt(artifact_key(art_id), Artifact)[0] is not None:
        return ctx.store.artifact_bytes(art_id)
    try:
        from assetstudio_processing.render import preview_png

        return preview_png((parts / "portable").read_bytes())
    except Exception:  # noqa: BLE001 - renderer limits/format quirks must never fail the publication
        log.info("preview render failed for %s", art_id, exc_info=True)
        return None


def _publish_roles(ctx: ProjectContext, who: Principal, req: CommitPublication, receipt: dict[str, Any], op_id: str,
                   asset_id: str, roles: dict[str, str], digest: str) -> Any:
    existing = None if req.target_asset_id is None else asset_id
    new = None if existing else NewAsset(_free_name(ctx, req.name, asset_id), req.name, Kind.model3d, Origin.imported,
                                         req.category_id, sorted(set(req.tags)))
    status = "unknown" if req.licence == "unknown" else "review"
    integration = {"actor": who.actor, "preview_id": req.preview_id, "package_sha256": req.package_sha256,
                   "request_sha256": digest, "credential_id": who.credential_id}
    try:
        return publish(ctx.store, PublishRequest(
            op_id=op_id, idempotency_key=req.idempotency_key, artifacts=roles,
            preview_role="preview" if "preview" in roles else None, origin=Origin.imported, kind=Kind.model3d,
            asset_id=existing, new_asset=new, expected_current_version=req.expected_current_version,
            details={"sources": {"integration": integration},
                     "validation": {"portable_sha256": req.portable_sha256, "budget": receipt["budget"],
                                    "source": receipt["source"] and {
                                        k: v for k, v in receipt["source"].items() if k != "source_surfaces"},
                                    "warnings": receipt["warnings"]},
                     "licence": {"status": status, "declared": req.licence, "credit": req.credit,
                                 "source_uri": req.source_uri,
                                 "note": "Published from Godot: rights are declared by the publisher, not verified."}},
            note="Published from Godot"))
    except StalePointer:
        current = ctx.store.get(manifest_key(asset_id), AssetManifest)[0].current_version_id
        raise _stale(current) from None
    except (Conflict, StorageError) as e:
        raise ServiceError(getattr(e, "code", "conflict"), str(e)[:300], status=409) from None


def _deliveries(studio: Studio, ctx: ProjectContext, server_id: str, asset_id: str, version_id: str,
                limits: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Best effort: a failure here never un-publishes; the next resolve prepares the deliveries."""
    try:
        found = svc.ensure_version(studio, ctx, server_id, asset_id, version_id, limits)
    except Exception:  # noqa: BLE001
        log.warning("delivery preparation failed for %s/%s", asset_id, version_id, exc_info=True)
        return []
    return [svc.summary(d) for d in found.deliveries] if found.state == "ready" else []


def _finalize(studio: Studio, ctx: ProjectContext, server_id: str, req: CommitPublication, payload: dict[str, Any],
              done: dict[str, Any], limits: dict[str, Any] | None) -> dict[str, Any]:
    asset_id, version_id = done["asset_id"], done["version_id"]
    deliveries = _deliveries(studio, ctx, server_id, asset_id, version_id, limits)
    ctx.index.upsert(ctx.store.get(manifest_key(asset_id), AssetManifest)[0])
    version = ctx.store.get(version_key(asset_id, version_id), AssetVersion)[0]
    response = {
        "operation": {"idempotency_key": req.idempotency_key, "op_id": _op_id(ctx, req.idempotency_key),
                      "state": "committed"},
        "asset_ref": {"server_id": server_id, "library_id": ctx.id, "asset_id": asset_id, "version_id": version_id},
        "display_version": done["display_version"], "descriptor_sha256": version.artifacts["descriptor"]["sha256"],
        "deliveries": deliveries}
    studio.journal.record_command(ctx.id, ACTION, req.idempotency_key, payload, response)
    discard(studio.settings, req.preview_id)  # deferred, idempotent: only after the response is durable
    studio.events.publish("library", project_id=ctx.id, asset_id=asset_id, change="published")
    return response
