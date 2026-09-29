"""What a variant (source-conditioned) Job generates from: its frozen plan and the plan's primary reference image.
Enhancement and generation both read it here so they cannot disagree about the conditioning input."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from assetstudio_core.domain import Job
from assetstudio_core.variants import ReferenceImage, VariantPlan
from assetstudio_storage.repo import CorruptBlob, IntegrityError, NotFound

from ..errors import ApiError
from ..registry import ProjectContext

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class SourceIntegrityError(Exception):
    """The plan's reference bytes are missing or differ from the frozen digest: never condition on them."""


@dataclass(frozen=True)
class VariantSource:
    plan: VariantPlan
    primary: ReferenceImage
    source_sha256: str  # the plan's source primary artifact (lineage), not the prepared input

    def preserve_text(self) -> str:
        return "; ".join(c.text for c in self.plan.preserve)

    def bindings(self) -> dict[str, str | None]:
        return {"plan_id": self.plan.id, "plan_sha256": self.plan.sha256,
                "reference_set_id": self.plan.reference_set_id, "primary_reference_sha256": self.primary.sha256}

    def conditioning(self) -> dict[str, str]:
        return {"artifact_id": self.primary.artifact_id, "sha256": self.primary.sha256, "view": self.primary.view}


def variant_source(ctx: ProjectContext, job: Job) -> VariantSource | None:
    """None for ordinary and direct-transform Jobs."""
    if not job.variant or job.direct:
        return None
    from .variant_jobs import load_plan

    plan = load_plan(ctx, job.variant["plan_id"])
    primary = next((r for r in plan.references if r.role == "primary"), None)
    if primary is None:
        raise ApiError(409, "references_missing", "the variant plan has no primary reference image")
    src = plan.source.artifact(plan.source.primary_role)
    return VariantSource(plan, primary, src.sha256 if src else plan.source.version_sha256)


def primary_bytes(ctx: ProjectContext, vs: VariantSource) -> bytes:
    try:
        data = ctx.store.artifact_bytes(vs.primary.artifact_id)
    except (CorruptBlob, IntegrityError, NotFound) as e:
        raise SourceIntegrityError(f"source reference {vs.primary.view} failed verification: {e}"[:300]) from e
    if hashlib.sha256(data).hexdigest() != vs.primary.sha256:
        raise SourceIntegrityError(f"source reference {vs.primary.view} differs from the frozen plan")
    if not data.startswith(PNG_MAGIC):
        raise SourceIntegrityError("the plan's prepared input predates PNG normalisation; "
                                   "create a new variant draft from this source")
    return data
