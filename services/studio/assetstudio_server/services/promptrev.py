"""Immutable prompt revisions: one place that composes the effective prompt from description + technical template."""
from __future__ import annotations

from typing import Any

from assetstudio_core.canonical import now_iso, sha256_json
from assetstudio_core.domain import JobItem, PromptRevision
from assetstudio_storage.project import ProjectStore

from .records import prompt_key


def compose(description: str, suffix: str) -> str:
    desc = description.strip().rstrip(" .,")
    return f"{desc}, {suffix}" if suffix.strip() else desc


def make_revision(store: ProjectStore, item: JobItem, *, rid: str, origin: str, description: str,
                  enhancer: dict[str, Any] | None = None) -> PromptRevision:
    """Immutable prompt revision. Existing id (retry) returns the stored record unchanged."""
    key = prompt_key(item.job_id, rid)
    existing, _ = store.get_opt(key, PromptRevision)
    if existing is not None:
        return existing
    snap = store.read_snapshot(item.snapshot_sha)
    style = snap.get("style") or {}
    negative = ", ".join(x for x in (snap.get("negative", ""), style.get("negative", "")) if x)
    rev = PromptRevision(
        id=rid, item_id=item.id, number=len(item.prompt_revisions) + 1, parent_id=item.current_prompt,
        created_at=now_iso(), origin=origin, original_brief=item.brief, enhancer=enhancer,  # type: ignore[arg-type]
        description=description.strip(), template=snap["template"], positive=compose(description, snap["template"]),
        negative=negative, style_sha=sha256_json(style) if style else None, snapshot_sha=item.snapshot_sha)
    store.create(key, rev)
    return rev
