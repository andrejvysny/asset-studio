"""Immutable prompt revisions: one place that composes the effective prompt from description + technical template."""
from __future__ import annotations

from typing import Any

from assetstudio_core.canonical import now_iso, sha256_json
from assetstudio_core.domain import JobItem, PromptRevision
from assetstudio_storage.project import ProjectStore

from .edit_templates import EDIT_NEGATIVE, FIXED_SENTENCE, edit_template
from .records import prompt_key
from .reference_bindings import ENHANCER_MAX_IMAGES, resolve_references


def compose(description: str, suffix: str) -> str:
    desc = description.strip().rstrip(" .,")
    return f"{desc}, {suffix}" if suffix.strip() else desc


def edit_texts(snap: dict[str, Any], description: str, bindings: dict[str, Any]) -> tuple[str, str, str]:
    """-> (template, positive, negative). Edit-mode revisions always lead with the fixed source sentence (added
    here, so hand-edited descriptions keep it) and use the kind's edit constraints instead of the T2I template."""
    if bindings.get("mode") != "edit":
        style = snap.get("style") or {}
        negative = ", ".join(x for x in (snap.get("negative", ""), style.get("negative", "")) if x)
        return snap["template"], compose(description, snap["template"]), negative
    desc = description.strip()
    if not desc.startswith(FIXED_SENTENCE):
        desc = f"{FIXED_SENTENCE} {desc}"
    template = edit_template(snap["recipe"]["kind"])
    return template, compose(desc, template), EDIT_NEGATIVE


def make_revision(store: ProjectStore, item: JobItem, *, rid: str, origin: str, description: str,
                  enhancer: dict[str, Any] | None = None, bindings: dict[str, Any] | None = None) -> PromptRevision:
    """Immutable prompt revision. Existing id (retry) returns the stored record unchanged.
    `bindings`: what it was written against (see PromptRevision.bindings); None keeps pre-bindings behaviour."""
    key = prompt_key(item.job_id, rid)
    existing, _ = store.get_opt(key, PromptRevision)
    if existing is not None:
        return existing
    snap = store.read_snapshot(item.snapshot_sha)
    style = snap.get("style") or {}
    bound = bindings or {}
    template, positive, negative = edit_texts(snap, description, bound)
    rev = PromptRevision(
        id=rid, item_id=item.id, number=len(item.prompt_revisions) + 1, parent_id=item.current_prompt,
        created_at=now_iso(), origin=origin, original_brief=item.brief, enhancer=enhancer,  # type: ignore[arg-type]
        description=description.strip(), template=template, positive=positive,
        negative=negative, style_sha=sha256_json(style) if style else None, snapshot_sha=item.snapshot_sha,
        bindings=bound)
    store.create(key, rev)
    return rev


def edited_bindings(store: ProjectStore, item: JobItem, variant: dict[str, Any] | None,
                    refresh: bool = True) -> dict[str, Any]:
    """Bindings for a hand-edited revision: the parent's enhancer facts stay, the binding fields are refreshed to
    what the item holds now (an edit is a deliberate answer to changed references). `variant`: plan bindings."""
    parent, _ = store.get_opt(prompt_key(item.job_id, item.current_prompt), PromptRevision) if item.current_prompt \
        else (None, None)
    old = parent.bindings if parent else {}
    if not refresh:
        return dict(old)
    sel = resolve_references(store, item, store.read_snapshot(item.snapshot_sha), "prompt_guidance",
                             ENHANCER_MAX_IMAGES - (1 if variant else 0))
    fresh = {"preset": item.enhance_preset, "mode": "edit" if variant else "t2i",
             "references_revision": item.references_revision, "reference_ids": sel.ids(),
             "references_excluded": sel.excluded_list(), **(variant or {})}
    return {**old, **fresh}
