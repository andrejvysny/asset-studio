"""Variant planning records (no scheduling): SourceAnalysis storage, suggestion label normalisation and the
additive draft writers used by the stage handlers. Kept free of coordinator imports (the handlers import this)."""
from __future__ import annotations

import json
import re
from typing import Any

from assetstudio_core.canonical import sha256_json
from assetstudio_core.ids import derived_id, validate_id
from assetstudio_storage.repo import Conflict, NotFound

from ..errors import ApiError
from ..registry import ProjectContext
from .variants import load_draft, save_draft

MAX_ANALYSIS_IMAGES = 4
ANALYZE_SETTINGS = {"schema": 1, "max_images": MAX_ANALYSIS_IMAGES}
MAX_LABEL = 60
VLM_MODEL = "qwen3_vl_8b_instruct"


def analysis_key(analysis_id: str) -> str:
    return f"source-analyses/{analysis_id}.json"


def analysis_id_for(reference_set_id: str, model: str, request: str) -> str:
    return derived_id("vsa", reference_set_id, model, sha256_json(ANALYZE_SETTINGS), request)


def read_analysis(ctx: ProjectContext, analysis_id: str) -> dict[str, Any] | None:
    validate_id(analysis_id, "vsa")
    try:
        rec: dict[str, Any] = json.loads(ctx.store.repo.read_object(analysis_key(analysis_id)).data)
    except NotFound:
        return None
    return rec


def normalise_label(raw: str) -> str:
    """'compact_pine' -> 'Compact pine' (the model emits snake_case slugs)."""
    text = re.sub(r"\s+", " ", raw.replace("_", " ").replace("-", " ")).strip()
    return (text[:1].upper() + text[1:])[:MAX_LABEL].rstrip()


def normalise_rows(rows: list[Any], count: int) -> list[dict[str, str]]:
    """Display labels, case-insensitively deduplicated, at most `count`; malformed rows are dropped."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for r in rows:
        if not isinstance(r, dict) or not isinstance(r.get("label"), str):
            continue
        label = normalise_label(r["label"])
        change = str(r.get("change_request") or "").strip()[:2000]
        if not label or label.casefold() in seen:
            continue
        seen.add(label.casefold())
        out.append({"label": label, "change_request": change})
    return out[:count]


def _update_draft(ctx: ProjectContext, draft_id: str, apply: Any) -> None:
    """Additive metadata write under the store lock; a moved-on revision is fine (a concurrent writer conflict
    is retried against the fresh draft). Materialized drafts are frozen and left alone."""
    for _ in range(3):
        with ctx.store.lock:
            draft, token = load_draft(ctx, draft_id)
            if draft.materialized is not None:
                return
            apply(draft)
            try:
                save_draft(ctx, draft, token)
                return
            except Conflict:
                continue
    raise ApiError(409, "stale_variant_plan", "draft kept changing; result not attached")


def attach_analysis(ctx: ProjectContext, draft_id: str, analysis_id: str) -> None:
    def apply(d: Any) -> None:
        d.analysis_id = analysis_id
    _update_draft(ctx, draft_id, apply)


def attach_suggestion(ctx: ProjectContext, draft_id: str, suggestion: dict[str, Any]) -> None:
    def apply(d: Any) -> None:
        d.suggestion = suggestion
    _update_draft(ctx, draft_id, apply)
