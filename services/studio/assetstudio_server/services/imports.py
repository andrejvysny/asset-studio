"""Reviewed import route: upload -> safe inspection -> mapping -> explicit commit (publishes an imported version)."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.domain import AssetManifest
from assetstudio_core.ids import derived_id, is_id, new_id
from assetstudio_core.kinds import Kind, Origin
from assetstudio_core.naming import slug
from assetstudio_processing import atlas
from assetstudio_processing.glb import validate_glb_bytes
from assetstudio_processing.images import ImageRejected, inspect_image, thumbnail_png
from assetstudio_storage.project import manifest_key
from assetstudio_storage.publication import NewAsset, PublishRequest, publish
from pydantic import BaseModel, Field

from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from . import imports_sets as sets

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
    first = files[0][0]
    result.update(size=sum(len(b) for _, b in files), filename=first if len(files) == 1 else f"{len(files)} files")
    return _stage_meta(d, import_id, ctx, result, first)


def commit(studio: Studio, ctx: ProjectContext, req: CommitImport) -> dict[str, Any]:
    ctx.require_writable()
    if (prior := studio.journal.command_result(req.idempotency_key, req.model_dump(mode="json"))) is not None:
        return prior
    d = _staging(studio, req.import_id)
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
    provenance = {"import_id": req.import_id, "source_name": meta["filename"], "source_uri": req.source_uri,
                  "credit": req.credit, "licence": req.licence}
    roles, validation = _roles(ctx, d, meta, req, provenance)
    taken = ctx.index.name_ids()
    name_id = slug(req.name)
    n = 2
    while req.target_asset_id is None and name_id in taken:
        name_id, n = f"{slug(req.name)}_{n}", n + 1
    if req.target_asset_id:
        manifest, _ = ctx.store.get(manifest_key(req.target_asset_id), AssetManifest)
        if manifest.kind != req.kind:
            raise ApiError(422, "kind_mismatch", f"target asset is {manifest.kind.value}")
    res = publish(ctx.store, PublishRequest(
        op_id=derived_id("op", req.idempotency_key), idempotency_key=req.idempotency_key, artifacts=roles,
        preview_role="preview" if "preview" in roles else None, origin=Origin.imported,
        asset_id=req.target_asset_id, expected_current_version=req.expected_current_version,
        new_asset=None if req.target_asset_id else NewAsset(name_id, req.name, req.kind, Origin.imported,
                                                            req.category_id, sorted(set(req.tags))),
        details={"sources": provenance, "validation": validation,
                 "licence": {"status": "unknown" if req.licence == "unknown" else "review",
                             "components": [{"id": "import", "name": meta["filename"], "licence": req.licence,
                                             "status": "unknown" if req.licence == "unknown" else "review"}],
                             "note": "Imported content: rights are as declared by the operator, not verified."}},
        note=f"Imported from {meta['filename']}"))
    ctx.index.upsert(ctx.store.get(manifest_key(res.asset_id), AssetManifest)[0])
    ctx.store.create_or_same(f"imports/{req.import_id}.json", {**provenance, "asset_id": res.asset_id,
                                                                "version_id": res.version_id, "roles": roles})
    shutil.rmtree(d, ignore_errors=True)
    response = {"asset_id": res.asset_id, "version_id": res.version_id, "display_version": res.display_version}
    studio.journal.record_command(req.idempotency_key, req.model_dump(mode="json"), response)
    studio.events.publish("library", project_id=ctx.id)
    return response


def _roles(ctx: ProjectContext, d: Path, meta: dict[str, Any], req: CommitImport,
           provenance: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    if meta["format"] == "frames":
        return sets.frame_roles(ctx, d, meta, req.kind, req.parameters, provenance)
    if meta["format"] == "material_bundle":
        return sets.material_roles(ctx, d, meta, req.map_roles, provenance)
    data = (d / "content").read_bytes()
    role = "model" if meta["format"] == "glb" else "image"
    art = ctx.store.register_artifact(data, role, meta["mime"], meta={"import": provenance}, source=provenance,
                                      expected_sha256=meta["sha256"])
    roles = {role: art.id}
    if role == "image":
        roles["preview"] = ctx.store.register_artifact(thumbnail_png(data), "preview", "image/png",
                                                       lineage=[art.id]).id
    return roles, meta["validation"]
