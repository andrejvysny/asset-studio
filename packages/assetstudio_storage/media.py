"""Media library records: one JSON per image, id derived from the content sha256. Mutations run under `store.lock`
with an expected revision, like families."""
from __future__ import annotations

from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.domain import MediaItem
from assetstudio_core.ids import derived_id

from .project import ProjectStore
from .repo import Conflict

MAX_TAGS = 20
MAX_TAG_LEN = 40


def media_key(mid: str) -> str:
    return f"media/{mid}.json"


def media_id_for(sha256: str) -> str:
    return derived_id("med", sha256)


def normalize_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for raw in tags:
        tag = raw.strip().lower()
        if not tag or tag in out:
            continue
        if len(tag) > MAX_TAG_LEN:
            raise ValueError(f"tag longer than {MAX_TAG_LEN} characters: {tag[:20]!r}...")
        out.append(tag)
    if len(out) > MAX_TAGS:
        raise ValueError(f"at most {MAX_TAGS} tags")
    return out


def check_source_url(url: str) -> str:
    if url and not url.startswith(("http://", "https://")):
        raise ValueError("source_url must start with http:// or https://")
    return url


def get_media(store: ProjectStore, mid: str) -> MediaItem:
    return store.get(media_key(mid), MediaItem)[0]


def list_media(store: ProjectStore) -> list[MediaItem]:
    return [m for mid in store.list_ids("media") if (m := store.get_opt(media_key(mid), MediaItem)[0])]


def add_media(store: ProjectStore, item: MediaItem) -> tuple[MediaItem, bool]:
    """Create-or-existing on the content-derived id. Returns (item, created)."""
    with store.lock:
        existing, _ = store.get_opt(media_key(item.id), MediaItem)
        if existing is not None:
            return existing, False
        try:
            store.create(media_key(item.id), item)
        except Conflict:  # another process created it between our read and create
            return store.get(media_key(item.id), MediaItem)[0], False
        return item, True


def update_media(store: ProjectStore, mid: str, expected_revision: int, **fields: Any) -> MediaItem:
    with store.lock:
        item, token = store.get(media_key(mid), MediaItem)
        if item.revision != expected_revision:
            raise Conflict(f"media changed (revision {item.revision})")
        for k, v in fields.items():
            setattr(item, k, v)
        item.revision += 1
        item.updated_at = now_iso()
        store.replace(media_key(mid), item, token)
        return item


def set_archived(store: ProjectStore, mid: str, expected_revision: int, archived: bool) -> MediaItem:
    with store.lock:
        item, token = store.get(media_key(mid), MediaItem)
        if (item.archived_at is not None) == archived:
            return item
        if item.revision != expected_revision:
            raise Conflict(f"media changed (revision {item.revision})")
        now = now_iso()
        item.archived_at = now if archived else None
        item.revision += 1
        item.updated_at = now
        store.replace(media_key(mid), item, token)
        return item
