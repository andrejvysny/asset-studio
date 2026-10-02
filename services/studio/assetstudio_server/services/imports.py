"""Reviewed import route: upload -> safe inspection -> mapping -> explicit commit (publishes an imported version)."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from assetstudio_core.canonical import now_iso, sha256_json
from assetstudio_core.domain import AssetManifest
from assetstudio_core.ids import derived_id, is_id, new_id
from assetstudio_core.kinds import Kind, Origin
from assetstudio_core.naming import slug
from assetstudio_processing import atlas
from assetstudio_processing.glb import validate_glb_bytes
from assetstudio_processing.images import ImageRejected, inspect_image, thumbnail_png
from assetstudio_storage.project import manifest_key
from assetstudio_storage.publication import NewAsset, PublishRequest, StalePointer, name_key, publish
from assetstudio_storage.repo import Conflict, StorageError
from pydantic import BaseModel, Field

from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from . import imports_sets as sets
from .records import cmd_payload

IMAGE_KINDS = (Kind.concept_art, Kind.sprite, Kind.icon, Kind.material)


class CommitImport(BaseModel):
    import_id: str
    name: str = Field(min_length=1, max_length=200)
    kind: Kind
    category_id: str | None = None
    tags: list[str] = Field(default=[], max_length=50)
    target_asset_id: str | None = None
    expected_current_version: str | None = None
    licence: str = Field(default="unknown", max_length=200)
    source_uri: str | None = Field(default=None, max_length=1000)
    credit: str | None = Field(default=None, max_length=500)
    map_roles: dict[str, str] = Field(default={}, max_length=16)  # material bundle: filename -> map role
    parameters: dict[str, Any] = Field(default={}, max_length=16)  # frame sequence: atlas parameters
    idempotency_key: str = Field(min_length=8, max_length=100)


def _staging(studio: Studio, import_id: str):
    if not is_id(import_id, "imp"):
        raise ApiError(400, "invalid_id", "invalid import id")
    return studio.settings.instance_dir / "staging" / "imports" / import_id


def preview(studio: Studio, ctx: ProjectContext, filename: str, data: bytes) -> dict[str, Any]:
    ctx.require_writable()
    if len(data) > studio.settings.max_upload_bytes:
        raise ApiError(413, "too_large", "file exceeds the upload limit")
    safe_name = _safe_name(filename)
    lower = safe_name.lower()
    result: dict[str, Any] = {"filename": safe_name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    if lower.endswith(".glb"):
        v = validate_glb_bytes(data, require_texture=False)
        result.update(format="glb", mime="model/gltf-binary", allowed_kinds=[Kind.model3d.value],
                      suggested_kind=Kind.model3d.value, validation=v, ok=v["ok"])
    elif lower.endswith((".png", ".jpg", ".jpeg")):
        try:
            info = inspect_image(data)
            result.update(format=info.format.lower(), mime=info.mime, image=info.as_meta(), ok=True,
                          validation={"ok": True, "checks": [{"id": "decode", "ok": True, "required": True}]},
                          allowed_kinds=[k.value for k in IMAGE_KINDS],
                          suggested_kind=(Kind.sprite if info.has_alpha else Kind.concept_art).value)
        except ImageRejected as e:
            result.update(ok=False, validation={"ok": False, "checks": [{"id": "decode", "ok": False,
                                                                         "detail": str(e), "required": True}]})
    elif lower.endswith(".zip"):
        return preview_set(studio, ctx, "frames", [(safe_name, data)])
    elif lower.endswith((".gltf", ".obj", ".fbx")):
        raise ApiError(422, "unsupported_format", "only self-contained .glb is accepted for 3D in this release")
    else:
        raise ApiError(422, "unsupported_format", "supported: .png, .jpg/.jpeg, .glb, .zip (frame sequence)")
    import_id, d = _new_staging(studio)
    (d / "content").write_bytes(data)
    return _stage_meta(d, import_id, ctx, result, safe_name)


def _safe_name(filename: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", filename.rsplit("/", 1)[-1])[:120] or "upload"


def _new_staging(studio: Studio) -> tuple[str, Path]:
    import_id = new_id("imp")
    d = _staging(studio, import_id)
    d.mkdir(parents=True)
    return import_id, d


def _stage_meta(d: Path, import_id: str, ctx: ProjectContext, result: dict[str, Any], name: str) -> dict[str, Any]:
    (d / "meta.json").write_text(json.dumps({**result, "project_id": ctx.id, "created_at": now_iso()}))
    return {"import_id": import_id, **result, "suggested_name": re.sub(r"[_-]+", " ", name.rsplit(".", 1)[0])}


def preview_set(studio: Studio, ctx: ProjectContext, mode: str, files: list[tuple[str, bytes]]) -> dict[str, Any]:
    """Several files (or one .zip of frames) reviewed as one import: an ordered frame sequence or a material bundle."""
    ctx.require_writable()
    if sum(len(b) for _, b in files) > studio.settings.max_upload_bytes:
        raise ApiError(413, "too_large", "files exceed the upload limit")
    files = [(_safe_name(n), b) for n, b in files]
    sets.reject_duplicate_names(files)
    if mode == "frames":
        try:
            staged = sets.expand(files)
        except atlas.FrameError as e:
            raise ApiError(422, "invalid_archive", str(e)) from e
        result = sets.preview_frames(staged)
    elif mode == "material":
        staged, result = files, sets.preview_material(files)
    else:
        raise ApiError(400, "invalid_mode", "mode must be frames or material")
    import_id, d = _new_staging(studio)
    result["staged"] = sets.stage(d, staged)
    result["upload_ids"] = [derived_id("upl", import_id, str(i)) for i in range(len(staged))]
    for m in result.get("maps", []):
        m["upload_id"] = result["upload_ids"][result["staged"].index(m["filename"])]
    first = files[0][0]
    result.update(size=sum(len(b) for _, b in files), filename=first if len(files) == 1 else f"{len(files)} files")
    return _stage_meta(d, import_id, ctx, result, first)


def _receipt_key(import_id: str) -> str:
    return f"imports/{import_id}.json"


def commit(studio: Studio, ctx: ProjectContext, req: CommitImport) -> dict[str, Any]:
    """Replay-safe: every identity derives from the import id, the durable receipt is written before staging is
    cleaned up, and a retry after cleanup finds that receipt instead of reporting an expired upload."""
    ctx.require_writable()
    payload = cmd_payload(req)
    prior = studio.journal.command_result(ctx.id, "import_commit", req.idempotency_key, payload)
    if prior is not None:
        return prior
    d = _staging(studio, req.import_id)
    receipt, _ = ctx.store.get_opt(_receipt_key(req.import_id), _Receipt)
    if receipt is not None:
        if receipt.request_sha256 != sha256_json(_import_request(req)):
            raise ApiError(409, "already_committed", "this upload was already committed with other settings")
        response = receipt.response()
    else:
        response = _commit_staged(ctx, d, req)
    studio.journal.record_command(ctx.id, "import_commit", req.idempotency_key, payload, response)
    shutil.rmtree(d, ignore_errors=True)  # deferred, idempotent: only after the receipt is durable
    studio.events.publish("library", project_id=ctx.id, asset_id=response["asset_id"], change="published")
    return response


class _Receipt(BaseModel):
    import_id: str
    request_sha256: str
    asset_id: str
    version_id: str
    display_version: int
    roles: dict[str, str]
    provenance: dict[str, Any]

    def response(self) -> dict[str, Any]:
        return {"asset_id": self.asset_id, "version_id": self.version_id, "display_version": self.display_version}


def _import_request(req: CommitImport) -> dict[str, Any]:
    """What an import commit is bound to (the client key may differ between a lost response and its retry)."""
    return req.model_dump(mode="json", exclude={"idempotency_key"})


def _commit_staged(ctx: ProjectContext, d: Path, req: CommitImport) -> dict[str, Any]:
    if not (d / "meta.json").is_file():
        raise ApiError(404, "unknown_import", "import preview expired; upload again")
    meta = json.loads((d / "meta.json").read_text())
    if meta["project_id"] != ctx.id:
        raise ApiError(409, "wrong_project", "this upload belongs to another project")
    if not meta.get("ok"):
        raise ApiError(422, "invalid_file", "the file failed structural validation", meta.get("validation"))
    if req.kind.value not in meta["allowed_kinds"]:
        raise ApiError(422, "kind_mismatch", f"a {meta['format']} file cannot be imported as {req.kind.value}")
    cfg, _ = ctx.config()
    if req.category_id is not None and cfg.category(req.category_id) is None:
        raise ApiError(422, "unknown_category", f"unknown category {req.category_id}")
    if req.target_asset_id:
        manifest, _ = ctx.store.get(manifest_key(req.target_asset_id), AssetManifest)
        if manifest.archived_at is not None:
            raise ApiError(409, "asset_archived", f"asset {manifest.asset_id} is archived; restore it first")
        if manifest.kind != req.kind:
            raise ApiError(422, "kind_mismatch", f"target asset is {manifest.kind.value}")
    provenance = {"import_id": req.import_id, "source_name": meta["filename"], "source_uri": req.source_uri,
                  "credit": req.credit, "licence": req.licence}
    roles, validation = _roles(ctx, d, meta, req, provenance,
                               lambda role: derived_id("art", ctx.id, req.import_id, role))
    op_id = derived_id("op", ctx.id, "import", req.import_id)
    name_id = _free_name(ctx, req.name) if req.target_asset_id is None else ""
    try:
        res = publish(ctx.store, PublishRequest(
            op_id=op_id, idempotency_key=req.import_id, artifacts=roles,
            preview_role="preview" if "preview" in roles else None, origin=Origin.imported, kind=req.kind,
            asset_id=req.target_asset_id, expected_current_version=req.expected_current_version,
            new_asset=None if req.target_asset_id else NewAsset(name_id, req.name, req.kind, Origin.imported,
                                                                req.category_id, sorted(set(req.tags))),
            details={"sources": provenance, "validation": validation,
                     "licence": {"status": "unknown" if req.licence == "unknown" else "review",
                                 "components": [{"id": "import", "name": meta["filename"], "licence": req.licence,
                                                 "status": "unknown" if req.licence == "unknown" else "review"}],
                                 "note": "Imported content: rights are as declared by the operator, not verified."}},
            note=f"Imported from {meta['filename']}"))
    except StalePointer as e:
        raise ApiError(409, "stale_pointer", str(e)) from e
    except (Conflict, StorageError) as e:
        raise ApiError(409, getattr(e, "code", "conflict"), str(e)) from e
    ctx.index.upsert(ctx.store.get(manifest_key(res.asset_id), AssetManifest)[0])
    receipt = _Receipt(import_id=req.import_id, request_sha256=sha256_json(_import_request(req)),
                       asset_id=res.asset_id, version_id=res.version_id, display_version=res.display_version,
                       roles=roles, provenance=provenance)
    ctx.store.create_or_same(_receipt_key(req.import_id), receipt)
    return receipt.response()


def _free_name(ctx: ProjectContext, name: str) -> str:
    """First free readable id by the authoritative name records (the index is only a cache)."""
    base, n = slug(name), 2
    name_id = base
    while ctx.store.repo.stat_object(name_key(name_id)) is not None:
        name_id, n = f"{base}_{n}", n + 1
    return name_id


def _roles(ctx: ProjectContext, d: Path, meta: dict[str, Any], req: CommitImport, provenance: dict[str, Any],
           aid: sets.ArtifactIds) -> tuple[dict[str, str], dict[str, Any]]:
    if meta["format"] == "frames":
        return sets.frame_roles(ctx, d, meta, req.kind, req.parameters, provenance, aid)
    if meta["format"] == "material_bundle":
        return sets.material_roles(ctx, d, meta, req.map_roles, provenance, aid)
    data = (d / "content").read_bytes()
    role = "model" if meta["format"] == "glb" else ("base_color" if req.kind == Kind.material else "image")
    art = ctx.store.register_artifact(data, role, meta["mime"], meta={"import": provenance}, source=provenance,
                                      expected_sha256=meta["sha256"], artifact_id=aid(role))
    roles = {role: art.id}
    if role != "model":
        roles["preview"] = ctx.store.register_artifact(thumbnail_png(data), "preview", "image/png",
                                                       lineage=[art.id], artifact_id=aid("preview")).id
    elif (png := _glb_preview(data)) is not None:
        roles["preview"] = ctx.store.register_artifact(png, "preview", "image/png", lineage=[art.id],
                                                       meta={"derived": "CPU render, 4 views, simplified shading"},
                                                       artifact_id=aid("preview")).id
    return roles, meta["validation"]


def _glb_preview(data: bytes) -> bytes | None:
    """A preview is a derivative: an unrenderable (e.g. over-budget) model still imports, just without one."""
    from assetstudio_processing.render import preview_png

    try:
        return preview_png(data)
    except Exception:  # noqa: BLE001 - renderer limits/format quirks must never fail the import
        return None
