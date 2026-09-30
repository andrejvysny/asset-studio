"""Moving files in: upload spool, media library, asset imports and the shot list."""
from __future__ import annotations

import mimetypes
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ..annotations import DESTRUCTIVE, READ, WRITE
from ..client import StudioClient
from ..deps import Deps, idem

MAX_MEDIA_BYTES = 64 * 1024 * 1024
SHOT_LIST_MAX_BYTES = 2 * 1024 * 1024


def _mime(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _metadata(name: str | None, note: str | None, tags: list[str] | None, rights: str | None,
              url: str | None) -> dict[str, Any]:
    fields = {"name": name, "note": note, "tags": tags, "source_rights": rights, "source_url": url}
    return {k: v for k, v in fields.items() if v is not None}


async def _media_revision(c: StudioClient, pid: str, media_id: str) -> int:
    return int((await c.get(f"/api/v1/projects/{pid}/media/{media_id}"))["revision"])


def _commit_fields(preview: dict[str, Any], name: str, kind: str | None, **rest: Any) -> dict[str, Any]:
    chosen = kind or preview.get("suggested_kind") or (preview.get("allowed_kinds") or [None])[0]
    if chosen is None:
        raise ToolError(f"import preview is not importable: {preview.get('validation')}")
    return {"import_id": preview["import_id"], "name": name, "kind": chosen, **rest}


def register(mcp: FastMCP, deps: Deps) -> None:
    @mcp.tool(annotations=WRITE)
    async def upload_file(ctx: Context, filename: str, data_base64: str) -> dict[str, Any]:
        """Stage a small file (inline base64, size-limited) and get an `upload_id`. The id is consumed by
        add_media, import_asset and shot_list_import; nothing is stored in a project yet. For large files use
        create_upload_url."""
        deps.client(ctx, write=True)
        return deps.files.put_inline(filename, data_base64).view()

    @mcp.tool(annotations=WRITE)
    async def create_upload_url(ctx: Context, filename: str) -> dict[str, Any]:
        """A one-time signed URL: HTTP PUT the raw file bytes to `url` (no multipart, no auth header; the URL
        works once for 15 minutes). The PUT response repeats the `upload_id`, which add_media, import_asset and
        shot_list_import consume."""
        deps.client(ctx, write=True)
        return deps.files.upload_url(filename)

    @mcp.tool(annotations=READ)
    async def search_media(ctx: Context, project_id: str | None = None, q: str = "", tag: str | None = None,
                           archived: bool = False) -> dict[str, Any]:
        """Media library (reference images for guidance, never production assets). `q` matches name, note and
        tags; `tag` filters by one tag; `archived=true` lists archived items. Returns `items` and tag counts."""
        c = deps.client(ctx)
        pid = await deps.project_id(c, project_id)
        return await c.get(f"/api/v1/projects/{pid}/media", q=q, tag=tag, archived=archived)

    @mcp.tool(annotations=WRITE)
    async def add_media(ctx: Context, upload_id: str, project_id: str | None = None, name: str | None = None,
                        note: str | None = None, tags: list[str] | None = None, source_rights: str | None = None,
                        source_url: str | None = None) -> dict[str, Any]:
        """Add an uploaded image (png/jpeg/webp) to the media library; identical bytes map to the same item
        (`duplicate=true`). Optional metadata is applied right after. The media_id is what Job items use as
        guidance reference."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        filename, data = deps.files.read(upload_id, MAX_MEDIA_BYTES)
        res = await c.post(f"/api/v1/projects/{pid}/media:upload", None,
                           files=[("files", (filename, data, _mime(filename)))])
        result = res["results"][0]
        if not result["ok"]:
            raise ToolError(f"{result['error']['code']}: {result['error']['message']}")
        item = result["item"]
        if meta := _metadata(name, note, tags, source_rights, source_url):
            item = await c.patch(f"/api/v1/projects/{pid}/media/{item['id']}",
                                 {"expected_revision": item["revision"], **meta})
        return {"item": item, "duplicate": result["duplicate"]}

    @mcp.tool(annotations=WRITE)
    async def update_media(ctx: Context, media_id: str, project_id: str | None = None, name: str | None = None,
                           note: str | None = None, tags: list[str] | None = None,
                           source_rights: str | None = None, source_url: str | None = None,
                           expected_revision: int | None = None) -> dict[str, Any]:
        """Change a media item's metadata; only the fields you pass change (`tags` replaces the list).
        `expected_revision` defaults to the current one."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        meta = _metadata(name, note, tags, source_rights, source_url)
        if not meta:
            raise ToolError("invalid_request: pass at least one field to change")
        rev = expected_revision if expected_revision is not None else await _media_revision(c, pid, media_id)
        return await c.patch(f"/api/v1/projects/{pid}/media/{media_id}", {"expected_revision": rev, **meta})

    @mcp.tool(annotations=DESTRUCTIVE)
    async def archive_media(ctx: Context, media_id: str, project_id: str | None = None, restore: bool = False,
                            expected_revision: int | None = None) -> dict[str, Any]:
        """Archive a media item (soft: hidden from search and unusable as guidance; bytes are kept) or, with
        `restore=true`, bring it back."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        rev = expected_revision if expected_revision is not None else await _media_revision(c, pid, media_id)
        action = "restore" if restore else "archive"
        return await c.post(f"/api/v1/projects/{pid}/media/{media_id}:{action}", {"expected_revision": rev})

    @mcp.tool(annotations=WRITE)
    async def import_asset(ctx: Context, upload_ids: list[str], name: str, project_id: str | None = None,
                           kind: str | None = None, category_id: str | None = None,
                           mode: Literal["single", "frames", "material"] = "single",
                           tags: list[str] | None = None, licence: str = "unknown", source_uri: str | None = None,
                           credit: str | None = None, target_asset_id: str | None = None,
                           expected_current_version: str | None = None,
                           map_roles: dict[str, str] | None = None, parameters: dict[str, Any] | None = None,
                           dry_run: bool = False, idempotency_key: str | None = None) -> dict[str, Any]:
        """Import existing files as a library asset: .glb (model3d), .png/.jpg (concept_art, sprite, icon,
        material) or a .zip of frames (sprite_sheet, vfx_flipbook). mode='single' takes exactly one upload;
        'frames' (ordered frame images) and 'material' (PBR map files) take several. `kind` defaults to the
        preview's suggestion. `dry_run=true` returns only the review (validation, allowed kinds, suggested kind,
        for material the detected maps: then set `map_roles` filename -> role). `parameters` are atlas options
        for frames (fps, padding, pow2). `target_asset_id` adds a new version to an existing asset. `licence`
        is recorded as declared, not verified. Retry with the same idempotency_key."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        base = f"/api/v1/projects/{pid}/imports"
        files = [deps.files.read(u, deps.settings.max_upload_bytes) for u in upload_ids]
        if mode == "single":
            if len(files) != 1:
                raise ToolError("invalid_request: mode='single' needs exactly one upload_id")
            fname, data = files[0]
            preview = await c.post(f"{base}:preview", None, files={"file": (fname, data, _mime(fname))})
        else:
            preview = await c.post(f"{base}:preview-set", None, params={"mode": mode},
                                   files=[("files", (n, d, _mime(n))) for n, d in files])
        if dry_run:
            return {"preview": preview}
        body = _commit_fields(preview, name, kind, category_id=category_id, tags=tags or [], licence=licence,
                              source_uri=source_uri, credit=credit, target_asset_id=target_asset_id,
                              expected_current_version=expected_current_version, map_roles=map_roles or {},
                              parameters=parameters or {}, idempotency_key=idem(idempotency_key))
        return {"preview": preview, "committed": await c.post(f"{base}:commit", body)}

    @mcp.tool(annotations=READ)
    async def shot_list_get(ctx: Context, project_id: str | None = None) -> dict[str, Any]:
        """The planned-shots list with its `revision` and each shot's status (planned, in_batch, done...) and
        effective kind. Pass the revision to shot_list_put."""
        c = deps.client(ctx)
        pid = await deps.project_id(c, project_id)
        return await c.get(f"/api/v1/projects/{pid}/shot-list")

    @mcp.tool(annotations=DESTRUCTIVE)
    async def shot_list_put(ctx: Context, items: list[dict[str, Any]], expected_revision: int,
                            project_id: str | None = None) -> dict[str, Any]:
        """Replace the WHOLE shot list (shots missing from `items` are removed). Each item: name (required), id
        (keep it to update an existing shot), category_id, kind, brief, priority (low|med|high), notes,
        target_asset_id, external_id, archived. `expected_revision` from shot_list_get is required
        (stale_shotlist otherwise). To add rows without touching others use shot_list_import."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        return await c.put(f"/api/v1/projects/{pid}/shot-list", {"expected_revision": expected_revision,
                                                                 "items": items})

    @mcp.tool(annotations=WRITE)
    async def shot_list_import(ctx: Context, text: str, filename: str = "shots.csv", dry_run: bool = False,
                               actions: dict[str, Literal["create", "update", "skip"]] | None = None,
                               project_id: str | None = None) -> dict[str, Any]:
        """Import shots from CSV/TSV/JSON text (`filename` decides the format). Columns: name, category, kind,
        brief, priority, notes, target_asset_id, id (external id: matching rows update, others are created).
        `dry_run=true` returns only the preview (per row: values, errors, default_action). `actions` maps a row
        `line` (as a string) to create|update|skip to override defaults. Rows with errors must be skipped."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        data = text.encode()
        if len(data) > SHOT_LIST_MAX_BYTES:
            raise ToolError(f"too_large: at most {SHOT_LIST_MAX_BYTES} bytes")
        base = f"/api/v1/projects/{pid}/shot-list"
        preview = await c.post(f"{base}:preview-import", None, files={"file": (filename, data, _mime(filename))})
        if dry_run:
            return {"preview": preview}
        committed = await c.post(f"{base}:commit-import", {"preview_id": preview["preview_id"],
                                                           "expected_revision": preview["revision"],
                                                           "actions": actions or {}})
        return {"preview": preview, "committed": committed}
