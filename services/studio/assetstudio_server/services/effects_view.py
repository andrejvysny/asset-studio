"""Per-item effect report: what this item's frozen snapshot is planned to do, and which reference images reach
which consumer. Read-only; the same selection the enhancer and compare-QA compute."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from assetstudio_core.effects import field_effects

from ..registry import ProjectContext
from .records import load_item, load_job
from .reference_bindings import COMPARE_MAX_REFERENCES, ENHANCER_MAX_IMAGES, Selection, resolve_references


def _selection(sel: Selection, limit: int) -> dict[str, Any]:
    return {"limit": limit, "selected": [asdict(b) for b in sel.selected], "excluded": sel.excluded_list()}


def item_effects(ctx: ProjectContext, job_id: str, item_id: str) -> dict[str, Any]:
    job, _ = load_job(ctx.store, job_id)
    item, _ = load_item(ctx.store, job_id, item_id)
    snap = ctx.store.read_snapshot(item.snapshot_sha)
    edit = job.variant is not None and not job.direct
    enhancer = ENHANCER_MAX_IMAGES - (1 if edit else 0)  # the variant source image takes one enhancer slot
    return {
        "planned": True, "item_id": item.id, "snapshot_sha": item.snapshot_sha, "mode": "edit" if edit else "t2i",
        "effects": [e.model_dump(mode="json") for e in field_effects(snap, "edit" if edit else "t2i")],
        "references": {
            "prompt_guidance": _selection(resolve_references(ctx.store, item, snap, "prompt_guidance", enhancer),
                                          enhancer),
            "qa_reference": _selection(resolve_references(ctx.store, item, snap, "qa_reference",
                                                          COMPARE_MAX_REFERENCES), COMPARE_MAX_REFERENCES),
        },
    }
