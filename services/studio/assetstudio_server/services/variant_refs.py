"""Source reference images for source-conditioned variants (no inference): neutral single-object renders of a GLB
or an alpha-composited copy of a 2D source, registered as derived, replay-safe artifacts."""
from __future__ import annotations

import io
import json
from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.ids import derived_id
from assetstudio_core.kinds import Kind
from assetstudio_core.variants import ReferenceImage, SourceBinding, VariantDraft
from assetstudio_processing.images import ImageRejected, inspect_image
from assetstudio_processing.render import REFERENCE_VIEWS, RENDERER_ID, render_view
from assetstudio_storage.repo import NotFound
from PIL import Image

from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from .variants import load_draft, primary_bytes, resolve_source, save_draft

RENDER_SIZE = 1024
NEUTRAL_BG = (200, 200, 200)
IMAGE_PROFILE = "assetstudio.image_prepare.v1"


def refs_key(srs_id: str) -> str:
    return f"source-references/{srs_id}.json"


def default_view(kind: Kind) -> str:
    return "three_quarter" if kind is Kind.model3d else "image"


def with_primary(images: list[dict[str, Any]], primary: str) -> list[ReferenceImage]:
    return [ReferenceImage(**{**i, "role": "primary" if i["view"] == primary else "auxiliary"}) for i in images]


def _unavailable(msg: str) -> ApiError:
    return ApiError(422, "reference_conditioning_unavailable", msg)


def _register(ctx: ProjectContext, source: SourceBinding, view: str, profile: str, data: bytes, mime: str,
              role: str, meta: dict[str, Any]) -> dict[str, Any]:
    src = source.artifact(source.primary_role)
    assert src is not None
    key = json.dumps({k: v for k, v in meta.items() if k != "warnings"}, sort_keys=True)
    art = ctx.store.register_artifact(data, role, mime, meta=meta, lineage=[src.artifact_id],
                                      artifact_id=derived_id("art", src.sha256, view, profile, key))
    return {"artifact_id": art.id, "sha256": art.sha256, "view": view, "params": meta}


def _render_3d(ctx: ProjectContext, source: SourceBinding) -> list[dict[str, Any]]:
    data = primary_bytes(ctx, source)
    out = []
    for view, (yaw, pitch) in REFERENCE_VIEWS.items():
        try:
            png, meta = render_view(data, yaw, pitch, RENDER_SIZE)
        except Exception as e:  # trimesh/numpy raise many types on hostile meshes; never crash the request
            raise _unavailable(f"cannot render the {view} view of the source: {str(e)[:200]}") from e
        out.append(_register(ctx, source, view, RENDERER_ID, png, "image/png", "source_render", meta))
    return out


def _prepare_2d(ctx: ProjectContext, source: SourceBinding) -> list[dict[str, Any]]:
    data = primary_bytes(ctx, source)
    try:
        info = inspect_image(data, ("PNG", "JPEG", "WEBP"))
    except ImageRejected as e:
        raise _unavailable(str(e)) from e
    meta: dict[str, Any] = {"width": info.width, "height": info.height, "format": info.format,
                            "alpha_composited": info.has_alpha, "warnings": []}
    mime = info.mime
    if info.has_alpha:
        with Image.open(io.BytesIO(data)) as im:
            rgba = im.convert("RGBA")
        base = Image.new("RGBA", rgba.size, (*NEUTRAL_BG, 255))
        buf = io.BytesIO()
        Image.alpha_composite(base, rgba).convert("RGB").save(buf, "PNG")
        data, mime = buf.getvalue(), "image/png"
        meta["background"] = list(NEUTRAL_BG)
    return [_register(ctx, source, "image", IMAGE_PROFILE, data, mime, "source_prepared", meta)]


def prepare_references(studio: Studio, ctx: ProjectContext, draft_id: str) -> dict[str, Any]:
    ctx.require_writable()
    draft, _ = load_draft(ctx, draft_id)
    if draft.materialized is not None:
        raise ApiError(409, "draft_materialized", "this draft was already saved as Jobs")
    source = resolve_source(ctx, draft.source.asset_id, draft.source.version_id)
    if source != draft.source:
        raise ApiError(409, "source_version_mismatch", "the source no longer matches the draft; start a new draft")
    is3d = source.kind is Kind.model3d
    images = _render_3d(ctx, source) if is3d else _prepare_2d(ctx, source)
    profile = f"{RENDERER_ID}:{RENDER_SIZE}" if is3d else IMAGE_PROFILE
    srs_id = derived_id("srs", source.version_sha256, profile)
    warnings = sorted({w for i in images for w in i["params"].get("warnings", [])})
    if ctx.store.repo.stat_object(refs_key(srs_id)) is None:
        ctx.store.create_or_same(refs_key(srs_id), {
            "id": srs_id, "source": source.model_dump(mode="json"),
            "images": [{**i, "role": "auxiliary"} for i in images],
            "renderer": {"id": RENDERER_ID if is3d else IMAGE_PROFILE, "size": RENDER_SIZE if is3d else None},
            "created_at": now_iso(), "warnings": warnings})
    primary = _update_draft(ctx, draft_id, srs_id, [i["view"] for i in images])
    return {"id": srs_id, "source": source.model_dump(mode="json"), "primary_view": primary,
            "images": [r.model_dump(mode="json") for r in with_primary(images, primary)], "warnings": warnings}


def _update_draft(ctx: ProjectContext, draft_id: str, srs_id: str, views: list[str]) -> str:
    with ctx.store.lock:
        draft, token = load_draft(ctx, draft_id)
        if draft.materialized is not None:
            raise ApiError(409, "draft_materialized", "this draft was already saved as Jobs")
        primary = draft.primary_view if draft.primary_view in views else default_view(draft.source.kind)
        draft.reference_set_id, draft.primary_view = srs_id, primary
        save_draft(ctx, draft, token)
        return primary


def load_reference_set(ctx: ProjectContext, draft: VariantDraft) -> tuple[dict[str, Any], list[ReferenceImage]]:
    """The draft's stored set with roles resolved against the draft's primary view."""
    if draft.reference_set_id is None:
        raise ApiError(409, "references_missing", "prepare the source reference images first")
    try:
        rec = json.loads(ctx.store.repo.read_object(refs_key(draft.reference_set_id)).data)
    except NotFound as e:
        raise ApiError(409, "references_missing", "the reference set is missing; prepare references again") from e
    if rec["source"]["version_sha256"] != draft.source.version_sha256:
        raise ApiError(409, "source_version_mismatch", "the reference set belongs to a different source version")
    views = [i["view"] for i in rec["images"]]
    primary = draft.primary_view if draft.primary_view in views else default_view(draft.source.kind)
    return rec, with_primary(rec["images"], primary)
