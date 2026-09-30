"""Immutable style revisions: one JSON per (style id, content sha). studio.yaml stays the mutable authority; these
records are history. The sha is the same `sha256_json(style)` that prompt revisions record as `style_sha`, so a Job's
prompt can be traced to the revision it was written against. Written after each config save and backfilled on read,
so a crash between the two writes only delays a record, never loses it."""
from __future__ import annotations

import json
from typing import Any

from assetstudio_core.canonical import now_iso, sha256_json
from assetstudio_core.config import StudioConfig
from assetstudio_core.ids import CONFIG_KEY_RE

from .project import ProjectStore
from .repo import Conflict


def style_revision_key(style_id: str, sha: str) -> str:
    return f"styles/{style_id}/{sha}.json"


def record_style_revisions(store: ProjectStore, cfg: StudioConfig, actor: str) -> None:
    """First appearance wins: re-saving (or restoring) identical content keeps the original record. Style ids are
    free-form in studio.yaml; only slug-like ids get history (they become storage key segments)."""
    for style_id, style in cfg.styles.items():
        if not CONFIG_KEY_RE.match(style_id):
            continue
        content = style.model_dump(mode="json")
        sha = sha256_json(content)
        record = {"style_id": style_id, "sha256": sha, "content": content, "config_revision": cfg.revision,
                  "created_at": now_iso(), "actor": actor}
        try:
            store.create(style_revision_key(style_id, sha), record)
        except Conflict:
            continue


def style_history(store: ProjectStore, cfg: StudioConfig, style_id: str, actor: str | None) -> list[dict[str, Any]]:
    """Newest first; `current` marks the content in studio.yaml now. `actor` None = read-only (no backfill)."""
    if not CONFIG_KEY_RE.match(style_id):
        return []
    if actor is not None:
        record_style_revisions(store, cfg, actor)
    current = sha256_json(cfg.styles[style_id].model_dump(mode="json")) if style_id in cfg.styles else None
    out = []
    for sha in store.list_ids(f"styles/{style_id}/"):
        rec = json.loads(store.repo.read_object(style_revision_key(style_id, sha)).data)
        out.append({**rec, "current": sha == current})
    return sorted(out, key=lambda r: (r["created_at"], r["config_revision"]), reverse=True)
