"""Assets, versions, artifacts, imports, shot list."""
from __future__ import annotations

import re
from typing import Any, Literal

from assetstudio_core.domain import AssetManifest
from assetstudio_core.ids import derived_id, validate_id
from assetstudio_storage.local import LocalBackend
from assetstudio_storage.project import manifest_key
from assetstudio_storage.publication import set_current, update_metadata
from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from ..errors import ApiError
from ..registry import ProjectContext
from ..services import imports as import_svc
from ..services import library as lib
from ..services import media as media_svc
from ..services import shotlist as shot_svc
from ..studio import Studio
from .deps import project, studio

router = APIRouter(prefix="/api/v1/projects/{project_id}")


@router.get("/categories")
def categories(ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    return {"categories": lib.category_tree(ctx)}


@router.get("/assets")
def assets(ctx: ProjectContext = Depends(project), category_id: str | None = None, kind: str | None = None,
           origin: str | None = None, q: str | None = Query(default=None, max_length=200),
           planned: bool = True, limit: int = Query(default=60, ge=1, le=500),
           offset: int = Query(default=0, ge=0), family_id: str | None = None,
           group_by: Literal["family"] | None = None, cursor: str | None = None) -> dict[str, Any]:
    if family_id:
        validate_id(family_id, "fam")
    return lib.list_assets(ctx, category_id=category_id, kind=kind, origin=origin, q=q, show_planned=planned,
                           limit=limit, offset=offset, family_id=family_id, group_by=group_by, cursor=cursor)


@router.get("/families")
def families(ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    return lib.families_list(ctx)


@router.get("/families/{family_id}")
def family(family_id: str, ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    validate_id(family_id, "fam")
    return lib.family_detail(ctx, family_id)


class PatchFamily(BaseModel):
    expected_revision: int
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


@router.patch("/families/{family_id}")
def patch_family(family_id: str, req: PatchFamily, ctx: ProjectContext = Depends(project),
                 s: Studio = Depends(studio)) -> dict[str, Any]:
    ctx.require_writable()
    validate_id(family_id, "fam")
    out = lib.rename_family(ctx, family_id, req.expected_revision, req.name, req.description)
    s.events.publish("library", project_id=ctx.id)
    return out


@router.get("/assets/{asset_id}")
def asset(asset_id: str, version: str | None = None, ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    validate_id(asset_id, "ast")
    return lib.asset_detail(ctx, asset_id, version)


@router.get("/assets/{asset_id}/versions")
def versions(asset_id: str, ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    validate_id(asset_id, "ast")
    m, _ = ctx.store.get(manifest_key(asset_id), AssetManifest)
    return {"current_version_id": m.current_version_id, "versions": [v.model_dump() for v in m.versions],
            "pointer_log": [p.model_dump() for p in m.pointer_log]}


class PatchAsset(BaseModel):
    expected_revision: int
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    category_id: str | None = None
    set_category: bool = False
    tags: list[str] | None = Field(default=None, max_length=50)


@router.patch("/assets/{asset_id}")
def patch_asset(asset_id: str, req: PatchAsset, ctx: ProjectContext = Depends(project),
                s: Studio = Depends(studio)) -> dict[str, Any]:
    ctx.require_writable()
    validate_id(asset_id, "ast")
    if req.set_category and req.category_id and ctx.config()[0].category(req.category_id) is None:
        raise ApiError(422, "unknown_category", req.category_id)
    m = update_metadata(ctx.store, asset_id, req.expected_revision, display_name=req.display_name,
                        category_id=req.category_id, tags=req.tags, set_category=req.set_category)
    ctx.index.upsert(m)
    s.events.publish("library", project_id=ctx.id, asset_id=asset_id, change="metadata")
    return m.model_dump(mode="json")


class SetCurrent(BaseModel):
    version_id: str
    expected_current_version: str | None
    reason: str = Field(default="", max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=100)


@router.post("/assets/{asset_id}:set-current")
def set_current_version(asset_id: str, req: SetCurrent, ctx: ProjectContext = Depends(project),
                        s: Studio = Depends(studio)) -> dict[str, Any]:
    ctx.require_writable()
    validate_id(asset_id, "ast")
    m = set_current(ctx.store, asset_id, req.version_id, req.expected_current_version,
                    derived_id("op", req.idempotency_key), req.reason)
    ctx.index.upsert(m)
    s.events.publish("library", project_id=ctx.id, asset_id=asset_id, change="current")
    return m.model_dump(mode="json")


@router.get("/artifacts/{artifact_id}")
def artifact_meta(artifact_id: str, ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    validate_id(artifact_id, "art")
    return ctx.store.artifact(artifact_id).model_dump(mode="json")


_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "model/gltf-binary": ".glb"}


def _download_name(art: Any, name: str | None) -> str:
    """`name` comes from the client (e.g. the media name): reduced to [A-Za-z0-9._-], max 80 chars."""
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "").strip("._-")[:80] or f"{art.role}-{art.sha256[:12]}"
    ext = _EXT.get(art.mime, "")
    return base if not ext or base.lower().endswith(ext) else base + ext


@router.get("/artifacts/{artifact_id}/content")
def artifact_content(artifact_id: str, request: Request, ctx: ProjectContext = Depends(project),
                     download: bool = False, name: str | None = None) -> Response:
    validate_id(artifact_id, "art")
    art = ctx.store.artifact(artifact_id)
    headers = {"Cache-Control": "private, max-age=31536000, immutable", "X-Content-SHA256": art.sha256,
               "X-Content-Type-Options": "nosniff"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{_download_name(art, name)}"'
    backend = ctx.store.repo
    backend.verify_blob(art.sha256, art.size)  # cached per file identity; corrupt bytes are never served
    if isinstance(backend, LocalBackend) and (path := backend.blob_path(art.sha256)) is not None:
        return FileResponse(path, media_type=art.mime, headers=headers)  # supports Range requests
    return Response(backend.read_blob_verified(art.sha256), media_type=art.mime, headers=headers)


@router.post("/imports:preview")
async def import_preview(file: UploadFile = File(...), ctx: ProjectContext = Depends(project),
                         s: Studio = Depends(studio)) -> dict[str, Any]:
    data = await file.read(s.settings.max_upload_bytes + 1)
    return import_svc.preview(s, ctx, file.filename or "upload", data)


@router.post("/imports:preview-set")
async def import_preview_set(mode: str = Query(pattern="^(frames|material)$"),
                             files: list[UploadFile] = File(...), ctx: ProjectContext = Depends(project),
                             s: Studio = Depends(studio)) -> dict[str, Any]:
    if len(files) > 1024:
        raise ApiError(422, "too_many_files", "at most 1024 files per import")
    budget, out = s.settings.max_upload_bytes, []
    for f in files:
        data = await f.read(budget + 1)
        budget -= len(data)
        if budget < 0:
            raise ApiError(413, "too_large", "files exceed the upload limit")
        out.append((f.filename or f"file{len(out)}", data))
    return import_svc.preview_set(s, ctx, mode, out)


@router.post("/imports:commit")
def import_commit(req: import_svc.CommitImport, ctx: ProjectContext = Depends(project),
                  s: Studio = Depends(studio)) -> dict[str, Any]:
    return import_svc.commit(s, ctx, req)


@router.get("/shot-list")
def get_shots(ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    return shot_svc.get_shotlist(ctx)


@router.put("/shot-list")
def put_shots(req: shot_svc.SaveShotList, ctx: ProjectContext = Depends(project),
              s: Studio = Depends(studio)) -> dict[str, Any]:
    return shot_svc.save_shotlist(s, ctx, req)


@router.post("/shot-list:preview-import")
async def shots_preview(file: UploadFile = File(...), ctx: ProjectContext = Depends(project),
                        s: Studio = Depends(studio)) -> dict[str, Any]:
    data = await file.read(2 * 1024 * 1024 + 1)
    return shot_svc.preview_import(s, ctx, file.filename or "upload", data)


@router.post("/shot-list:commit-import")
def shots_commit(req: shot_svc.CommitImport, ctx: ProjectContext = Depends(project),
                 s: Studio = Depends(studio)) -> dict[str, Any]:
    return shot_svc.commit_import(s, ctx, req)


@router.post("/references:upload")
async def upload_reference(file: UploadFile = File(...), ctx: ProjectContext = Depends(project),
                           s: Studio = Depends(studio)) -> dict[str, Any]:
    """Registers the image in the media library (deduplicated by content) and returns its reference artifact.
    Adding it to a Job item is a separate step."""
    ctx.require_writable()
    data = await file.read(64 * 1024 * 1024 + 1)
    item, duplicate = media_svc.ingest(ctx, file.filename or "upload", data)
    meta = ctx.store.artifact(item.artifact_id).meta
    if not duplicate:
        s.events.publish("media", project_id=ctx.id)
    return {"artifact_id": item.artifact_id, "sha256": item.sha256, "format": meta["format"],
            "width": meta["width"], "height": meta["height"], "mode": meta["mode"],
            "has_alpha": meta["has_alpha"], "media_id": item.id, "duplicate": duplicate}
