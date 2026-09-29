"""Asset variants (Phase B1): capabilities, drafts, source references and plan -> Jobs. Nothing here runs inference."""
from __future__ import annotations

from typing import Any

from assetstudio_core.ids import validate_id
from assetstudio_core.variants import work_summary
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..registry import ProjectContext
from ..services import variant_jobs, variant_refs, variants
from ..studio import Studio
from .deps import project, studio

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["variants"])


def _draft_body(s: Studio, ctx: ProjectContext, draft: variants.VariantDraft, with_caps: bool) -> dict[str, Any]:
    body = draft.model_dump(mode="json")
    if with_caps:
        body["work"] = work_summary(draft.method, draft.rows)
        body["capabilities"] = variants.capabilities(s, ctx, draft.source.asset_id, draft.source.version_id)
    return body


@router.get("/assets/{asset_id}/versions/{version_id}/variant-capabilities")
def capabilities(asset_id: str, version_id: str, ctx: ProjectContext = Depends(project),
                 s: Studio = Depends(studio)) -> dict[str, Any]:
    validate_id(asset_id, "ast")
    validate_id(version_id, "ver")
    return variants.capabilities(s, ctx, asset_id, version_id)


@router.post("/variant-drafts", status_code=201)
def create_draft(req: variants.CreateDraft, ctx: ProjectContext = Depends(project),
                 s: Studio = Depends(studio)) -> Any:
    return JSONResponse(_draft_body(s, ctx, variants.create_draft(s, ctx, req), False), status_code=201)


@router.get("/variant-drafts/{draft_id}")
def get_draft(draft_id: str, ctx: ProjectContext = Depends(project), s: Studio = Depends(studio)) -> dict[str, Any]:
    return _draft_body(s, ctx, variants.load_draft(ctx, draft_id)[0], True)


@router.patch("/variant-drafts/{draft_id}")
def patch_draft(draft_id: str, req: variants.PatchDraft, ctx: ProjectContext = Depends(project),
                s: Studio = Depends(studio)) -> dict[str, Any]:
    return _draft_body(s, ctx, variants.patch_draft(ctx, draft_id, req), False)


@router.post("/variant-drafts/{draft_id}:prepare-references")
def prepare_references(draft_id: str, ctx: ProjectContext = Depends(project),
                       s: Studio = Depends(studio)) -> dict[str, Any]:
    return variant_refs.prepare_references(s, ctx, draft_id)


@router.post("/variant-drafts/{draft_id}:create-jobs", status_code=201)
def create_jobs(draft_id: str, req: variant_jobs.CreateJobs, ctx: ProjectContext = Depends(project),
                s: Studio = Depends(studio)) -> dict[str, Any]:
    return variant_jobs.create_jobs(s, ctx, draft_id, req)
