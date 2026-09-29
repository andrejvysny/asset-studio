"""Variant plan reports: advisory diversity of the current selection. Nothing here approves or changes a Job."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..registry import ProjectContext
from ..services import diversity
from ..studio import Studio
from .deps import project, studio

router = APIRouter(prefix="/api/v1/projects/{project_id}/variant-plans", tags=["variant-plans"])


@router.post("/{plan_id}:compare-selection")
def compare_selection(plan_id: str, req: diversity.CompareSelection, ctx: ProjectContext = Depends(project),
                      s: Studio = Depends(studio)) -> Any:
    return JSONResponse(diversity.compare_selection(s, ctx, plan_id, req), status_code=202)


@router.get("/{plan_id}/diversity")
def get_diversity(plan_id: str, ctx: ProjectContext = Depends(project)) -> dict[str, Any]:
    return diversity.diversity_status(ctx, plan_id)
