"""Media library: content-addressed reference images for brainstorming. Guidance only, never production assets."""
from __future__ import annotations

import io
from collections import Counter
from hashlib import sha256
from pathlib import Path
from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.domain import MediaItem
from assetstudio_core.ids import InvalidId, validate_id
from assetstudio_processing.images import ImageInfo, ImageRejected, inspect_image, thumbnail_png
from assetstudio_storage.media import add_media, list_media, media_id_for, media_key, normalize_tags
from assetstudio_storage.repo import NotFound
from PIL import Image, ImageOps

from ..errors import ApiError
from ..registry import ProjectContext

ALLOWED = ("PNG", "JPEG", "WEBP")
NAME_MAX = 120


def _webp_to_png(data: bytes, has_alpha: bool) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGBA" if has_alpha else "RGB")
        out = io.BytesIO()
        im.save(out, "PNG")
        return out.getvalue()


def _thumb(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        upright = ImageOps.exif_transpose(im)
        if upright.mode not in ("RGB", "RGBA", "L", "LA", "P"):  # CMYK/YCbCr JPEGs cannot be written as PNG
            upright = upright.convert("RGB")
        out = io.BytesIO()
        upright.save(out, "PNG")
    return thumbnail_png(out.getvalue())


def _default_name(filename: str) -> str:
    return Path(filename).stem.strip()[:NAME_MAX].strip() or "image"


def _inspect(data: bytes) -> tuple[bytes, ImageInfo, str | None]:
    try:
        info = inspect_image(data, ALLOWED)
        if info.format != "WEBP":
            return data, info, None
        png = _webp_to_png(data, info.has_alpha)
        return png, inspect_image(png, ALLOWED), "WEBP"
    except ImageRejected as e:
        raise ApiError(422, "invalid_image", str(e)) from e


def ingest(ctx: ProjectContext, filename: str, data: bytes) -> tuple[MediaItem, bool]:
    """Returns (item, duplicate). WEBP is stored as PNG so downstream consumers only see PNG/JPEG."""
    stored, info, source_format = _inspect(data)
    store = ctx.store
    mid = media_id_for(sha256(stored).hexdigest())
    existing = store.get_opt(media_key(mid), MediaItem)[0]
    if existing is not None:
        return existing, True
    meta: dict[str, Any] = {**info.as_meta(), "source_name": filename}
    if source_format:
        meta["source_format"] = source_format
    art = store.register_artifact(stored, "reference", info.mime, meta=meta)
    try:
        thumb = store.register_artifact(_thumb(stored), "preview", "image/png", lineage=[art.id])
    except (OSError, ValueError) as e:
        raise ApiError(422, "invalid_image", f"thumbnail failed: {e}") from e
    now = now_iso()
    item = MediaItem(id=mid, artifact_id=art.id, sha256=art.sha256, thumb_artifact_id=thumb.id,
                     name=_default_name(filename), format=info.format, width=info.width, height=info.height,
                     size=art.size, has_alpha=info.has_alpha, created_at=now, updated_at=now)
    item, created = add_media(store, item)
    return item, not created


def list_items(ctx: ProjectContext, q: str, tag: str | None, archived: bool) -> dict[str, Any]:
    pool = [m for m in list_media(ctx.store) if (m.archived_at is not None) == archived]
    counts = Counter(t for m in pool for t in m.tags)
    tags = [{"tag": t, "count": n} for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    want = (normalize_tags([tag]) or [None])[0] if tag else None
    needle = q.strip().lower()
    shown = [m for m in pool if (want is None or want in m.tags)
             and (not needle or needle in " ".join([m.name, m.note, *m.tags]).lower())]
    shown.sort(key=lambda m: m.id)
    shown.sort(key=lambda m: m.created_at, reverse=True)
    return {"items": [m.model_dump(mode="json") for m in shown], "tags": tags}


def resolve_media_artifact(ctx: ProjectContext, media_id: str) -> str:
    try:
        validate_id(media_id, "med")
    except InvalidId as e:
        raise ApiError(422, "invalid_media_id", str(e)) from e
    item = ctx.store.get_opt(media_key(media_id), MediaItem)[0]
    if item is None:
        raise ApiError(404, "unknown_media", f"media {media_id} does not exist")
    if item.archived_at is not None:
        raise ApiError(409, "media_archived", f"media {media_id} is archived; restore it first")
    return item.artifact_id


def count_active(ctx: ProjectContext) -> int:
    return sum(1 for m in list_media(ctx.store) if m.archived_at is None)


def get_item(ctx: ProjectContext, media_id: str) -> MediaItem:
    validate_id(media_id, "med")
    try:
        return ctx.store.get(media_key(media_id), MediaItem)[0]
    except NotFound as e:
        raise ApiError(404, "unknown_media", f"media {media_id} does not exist") from e
