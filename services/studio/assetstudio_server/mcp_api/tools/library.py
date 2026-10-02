"""Library tools: browse and search assets, curate metadata and families, inspect and fetch artifacts."""
from __future__ import annotations

import io
import json
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.utilities.types import Image
from mcp.types import TextContent
from PIL import Image as PILImage

from ..annotations import READ, WRITE
from ..client import StudioClient
from ..deps import Deps, compact, idem

IMAGE_MIMES = {"image/png": "png", "image/jpeg": "jpeg", "image/webp": "webp"}
MAX_SOURCE_IMAGE = 64 * 1024 * 1024  # decode cap; stored images were already inspected on ingest


def _viewable(data: bytes, max_px: int) -> Image:
    """Downscaled PNG for the agent's vision (full resolution costs context and adds little for review)."""
    with PILImage.open(io.BytesIO(data)) as im:
        im.thumbnail((max_px, max_px))
        out = io.BytesIO()
        im.save(out, "PNG", optimize=True)
    return Image(data=out.getvalue(), format="png")


async def _asset_revision(c: StudioClient, pid: str, asset_id: str) -> int:
    return int((await c.get(f"/api/v1/projects/{pid}/assets/{asset_id}"))["manifest"]["revision"])


def register(mcp: FastMCP, deps: Deps) -> None:
    @mcp.tool(annotations=READ)
    async def list_categories(ctx: Context, project_id: str | None = None) -> dict[str, Any]:
        """The category tree (ids, labels, parents, asset counts). Edit categories with config_set."""
        c = deps.client(ctx)
        pid = await deps.project_id(c, project_id)
        return await c.get(f"/api/v1/projects/{pid}/categories")

    @mcp.tool(annotations=READ)
    async def search_assets(ctx: Context, project_id: str | None = None, q: str | None = None,
                            category_id: str | None = None, kind: str | None = None, origin: str | None = None,
                            family_id: str | None = None, group_by: Literal["family"] | None = None,
                            planned: bool = True, limit: int = 60, offset: int = 0,
                            cursor: str | None = None, archived: bool = False) -> dict[str, Any]:
        """Search published assets (and planned shot-list entries when `planned`). `q` is free text over names,
        tags and family names; `category_id` includes sub-categories; `kind` e.g. model3d, sprite, icon, material,
        concept_art, sprite_sheet, vfx_flipbook; `origin` generated or imported. Offset paging returns `items`
        and `total`; group_by='family' returns `groups` and `next_cursor` (pass it back as `cursor`
        with the same filters until it is null). Archived assets are hidden unless `archived=true` (then only
        archived ones are returned)."""
        c = deps.client(ctx)
        pid = await deps.project_id(c, project_id)
        return await c.get(f"/api/v1/projects/{pid}/assets", q=q, category_id=category_id, kind=kind, origin=origin,
                           family_id=family_id, group_by=group_by, planned=planned, limit=limit, offset=offset,
                           cursor=cursor, archived=archived)

    @mcp.tool(annotations=READ)
    async def get_asset(ctx: Context, asset_id: str, project_id: str | None = None, version_id: str | None = None,
                        include_versions: bool = True) -> dict[str, Any]:
        """One asset: manifest, files (artifact ids by role), facts, provenance, QA and family for the current
        version (or `version_id`). `include_versions` adds the version list and the pointer log (history of
        which version was current). Fetch files with get_artifact / get_download_url."""
        c = deps.client(ctx)
        pid = await deps.project_id(c, project_id)
        out = compact(await c.get(f"/api/v1/projects/{pid}/assets/{asset_id}", version=version_id),
                      ("manifest_json",))
        if include_versions:
            out["version_history"] = await c.get(f"/api/v1/projects/{pid}/assets/{asset_id}/versions")
        return out

    @mcp.tool(annotations=WRITE)
    async def update_asset(ctx: Context, asset_id: str, project_id: str | None = None,
                           display_name: str | None = None, tags: list[str] | None = None,
                           category_id: str | None = None, set_category: bool = False,
                           expected_revision: int | None = None) -> dict[str, Any]:
        """Rename, retag or recategorize an asset (metadata only; versions and files are immutable).
        `tags` replaces the whole tag list. To change the category pass `set_category=true` with `category_id`
        (category_id=null with set_category=true clears it). `expected_revision` defaults to the current one."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        rev = expected_revision if expected_revision is not None else await _asset_revision(c, pid, asset_id)
        body = {"expected_revision": rev, "display_name": display_name, "tags": tags, "category_id": category_id,
                "set_category": set_category}
        return await c.patch(f"/api/v1/projects/{pid}/assets/{asset_id}", body)

    @mcp.tool(annotations=WRITE)
    async def move_assets(ctx: Context, asset_ids: list[str], category_id: str | None = None,
                          project_id: str | None = None) -> dict[str, Any]:
        """Move up to 500 assets to one category in a single call (category_id=null returns them to
        Uncategorized). Metadata only. Returns a per-asset result; unchanged assets are reported, not rewritten."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        return await c.post(f"/api/v1/projects/{pid}/assets:set-category",
                            {"asset_ids": asset_ids, "category_id": category_id})

    @mcp.tool(annotations=WRITE)
    async def archive_asset(ctx: Context, asset_id: str, project_id: str | None = None,
                            expected_revision: int | None = None) -> dict[str, Any]:
        """Archive an asset: hidden from the library, search and pickers, nothing deleted; restore_asset undoes it."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        rev = expected_revision if expected_revision is not None else await _asset_revision(c, pid, asset_id)
        return await c.post(f"/api/v1/projects/{pid}/assets/{asset_id}:archive", {"expected_revision": rev})

    @mcp.tool(annotations=WRITE)
    async def restore_asset(ctx: Context, asset_id: str, project_id: str | None = None,
                            expected_revision: int | None = None) -> dict[str, Any]:
        """Restore an archived asset to the active library (same id, versions and files)."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        rev = expected_revision if expected_revision is not None else await _asset_revision(c, pid, asset_id)
        return await c.post(f"/api/v1/projects/{pid}/assets/{asset_id}:restore", {"expected_revision": rev})

    @mcp.tool(annotations=WRITE)
    async def set_current_version(ctx: Context, asset_id: str, version_id: str, project_id: str | None = None,
                                  expected_current_version: str | None = None, reason: str = "",
                                  idempotency_key: str | None = None) -> dict[str, Any]:
        """Point the asset at an existing version (roll back or forward). `expected_current_version` defaults to
        the version that is current now; a 409 means it moved meanwhile."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        if expected_current_version is None:
            hist = await c.get(f"/api/v1/projects/{pid}/assets/{asset_id}/versions")
            expected_current_version = hist["current_version_id"]
        return await c.post(f"/api/v1/projects/{pid}/assets/{asset_id}:set-current", {
            "version_id": version_id, "expected_current_version": expected_current_version, "reason": reason,
            "idempotency_key": idem(idempotency_key)})

    @mcp.tool(annotations=READ)
    async def list_families(ctx: Context, project_id: str | None = None,
                            family_id: str | None = None) -> dict[str, Any]:
        """Asset families (variant groups) with member counts; with `family_id`, that family's detail."""
        c = deps.client(ctx)
        pid = await deps.project_id(c, project_id)
        path = f"/api/v1/projects/{pid}/families" + (f"/{family_id}" if family_id else "")
        return await c.get(path)

    @mcp.tool(annotations=WRITE)
    async def update_family(ctx: Context, family_id: str, project_id: str | None = None, name: str | None = None,
                            description: str | None = None, expected_revision: int | None = None) -> dict[str, Any]:
        """Rename a family or change its description. `expected_revision` defaults to the current one."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        path = f"/api/v1/projects/{pid}/families/{family_id}"
        rev = expected_revision if expected_revision is not None else (await c.get(path))["revision"]
        return await c.patch(path, {"expected_revision": rev, "name": name, "description": description})

    @mcp.tool(annotations=READ)
    async def get_artifact(ctx: Context, artifact_id: str, project_id: str | None = None,
                           include_image: bool = False, max_px: int = 768) -> Any:
        """Artifact metadata (role, mime, size, sha256, lineage, format facts). With `include_image=true` and a
        png/jpeg/webp artifact, the image follows the metadata so you can look at it, downscaled to fit
        `max_px` (64-2048). Originals and non-image files (GLB, npz): use get_download_url."""
        c = deps.client(ctx)
        pid = await deps.project_id(c, project_id)
        base = f"/api/v1/projects/{pid}/artifacts/{artifact_id}"
        meta = await c.get(base)
        if not include_image:
            return meta
        fmt = IMAGE_MIMES.get(meta["mime"])
        if fmt is None or meta["size"] > MAX_SOURCE_IMAGE:
            raise ToolError(f"not_inlinable: {meta['mime']} of {meta['size']} bytes; use get_download_url")
        data, _ = await c.download(f"{base}/content")
        image = _viewable(data, max(64, min(max_px, 2048)))
        return [TextContent(type="text", text=json.dumps(meta)), image]

    @mcp.tool(annotations=READ)
    async def get_download_url(ctx: Context, artifact_id: str, project_id: str | None = None) -> dict[str, Any]:
        """A signed, time-limited HTTP GET URL for an artifact's bytes (no auth header needed; supports Range).
        Use it for models and large files; the sha256 is in get_artifact."""
        c = deps.client(ctx)
        pid = await deps.project_id(c, project_id)
        meta = await c.get(f"/api/v1/projects/{pid}/artifacts/{artifact_id}")
        return {**deps.files.download_url(pid, artifact_id), "artifact_id": artifact_id, "sha256": meta["sha256"],
                "size": meta["size"], "mime": meta["mime"]}
