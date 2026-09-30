"""Project media library: tagged, archivable reference images that Job items can pick as guidance."""
from __future__ import annotations

from typing import Any

from assetstudio_storage.media import check_source_url, normalize_tags, set_archived, update_media
from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel, Field

from ..errors import ApiError
from ..registry import ProjectContext
from ..services import media as media_svc
from ..studio import Studio
from .deps import project, studio

router = APIRouter(prefix="/api/v1/projects/{project_id}")
MAX_FILES = 20
MAX_BYTES = 64 * 1024 * 1024


@router.get("/media")
def list_media(q: str = "", tag: str | None = None, archived: bool = False,
               ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    return media_svc.list_items(ctx, q, tag, archived)


async def _upload_one(ctx: ProjectContext, f: UploadFile) -> dict[str, Any]:
    name = f.filename or "upload"
    data = await f.read(MAX_BYTES + 1)
    try:
        if len(data) > MAX_BYTES:
            raise ApiError(413, "too_large", f"{name} exceeds {MAX_BYTES // (1024 * 1024)} MiB")
        item, duplicate = media_svc.ingest(ctx, name, data)
    except ApiError as e:
        return {"filename": name, "ok": False, "error": {"code": e.code, "message": e.message}}
    return {"filename": name, "ok": True, "item": item.model_dump(mode="json"), "duplicate": duplicate,
            "archived": item.archived_at is not None}


@router.post("/media:upload")
async def upload(files: list[UploadFile] = File(...), ctx: ProjectContext = Depends(project),
                 s: Studio = Depends(studio)) -> dict[str, Any]:
    ctx.require_writable()
    if len(files) > MAX_FILES:
        raise ApiError(422, "too_many_files", f"at most {MAX_FILES} files per upload")
    results = [await _upload_one(ctx, f) for f in files]
    if any(r["ok"] for r in results):
        s.events.publish("media", project_id=ctx.id)
    return {"results": results}


@router.get("/media/{media_id}")
def get_media(media_id: str, ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    return media_svc.get_item(ctx, media_id).model_dump(mode="json")


class MediaPatch(BaseModel):
    expected_revision: int
    name: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = None
    source_rights: str | None = Field(default=None, max_length=200)
    source_url: str | None = Field(default=None, max_length=500)


def _patch_fields(req: MediaPatch) -> dict[str, Any]:
    sent = {k: getattr(req, k) for k in req.model_fields_set - {"expected_revision"}}
    if any(v is None for v in sent.values()):
        raise ValueError("fields cannot be null; omit a field to leave it unchanged")
    if "name" in sent:
        sent["name"] = sent["name"].strip()
        if not sent["name"]:
            raise ValueError("name must not be empty")
    if "tags" in sent:
        sent["tags"] = normalize_tags(sent["tags"])
    if "source_url" in sent:
        sent["source_url"] = check_source_url(sent["source_url"].strip())
    return sent


@router.patch("/media/{media_id}")
def patch_media(media_id: str, req: MediaPatch, ctx: ProjectContext = Depends(project),
                s: Studio = Depends(studio)) -> dict[str, Any]:
    ctx.require_writable()
    media_svc.get_item(ctx, media_id)
    try:
        fields = _patch_fields(req)
    except ValueError as e:
        raise ApiError(422, "invalid_media", str(e)) from e
    out = update_media(ctx.store, media_id, req.expected_revision, **fields)
    s.events.publish("media", project_id=ctx.id)
    return out.model_dump(mode="json")


class Revision(BaseModel):
    expected_revision: int


def _archive(media_id: str, req: Revision, ctx: ProjectContext, s: Studio, archived: bool) -> dict[str, Any]:
    ctx.require_writable()
    media_svc.get_item(ctx, media_id)
    out = set_archived(ctx.store, media_id, req.expected_revision, archived)
    s.events.publish("media", project_id=ctx.id)
    return out.model_dump(mode="json")


@router.post("/media/{media_id}:archive")
def archive_media(media_id: str, req: Revision, ctx: ProjectContext = Depends(project),
                  s: Studio = Depends(studio)) -> dict[str, Any]:
    return _archive(media_id, req, ctx, s, True)


@router.post("/media/{media_id}:restore")
def restore_media(media_id: str, req: Revision, ctx: ProjectContext = Depends(project),
                  s: Studio = Depends(studio)) -> dict[str, Any]:
    return _archive(media_id, req, ctx, s, False)
