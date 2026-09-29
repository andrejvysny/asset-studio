"""Asset variants and families: pure records, capability rules and plan validation (no storage, no I/O).

A variant is a separate asset created from ONE exact published source version. One variant row becomes one
one-item Job (design: "One Job = one asset"); the rows of one plan share an immutable VariantPlan record and are
grouped into a draft Batch. Family membership lives only on AssetManifest.family_id (never a member list here).
"""
from __future__ import annotations

import math
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .kinds import Kind

VARIANT_SCHEMA = 1
MAX_ROWS = 32
DEFAULT_ROWS = 6
MAX_CANDIDATES = 8
DEFAULT_CANDIDATES = 4
MAX_TARGET_HEIGHT_M = 10_000.0
MAX_SCALE = 1_000.0
MIN_SCALE = 1e-3
RASTER_MAX_SIDE = 8192


class Method(StrEnum):
    image_edit_reconstruct = "image_edit_reconstruct"  # model3d: source render -> image edit -> TRELLIS.2 rebuild
    image_edit = "image_edit"  # 2D kinds: source image -> image edit -> kind build
    direct_transform = "direct_transform"  # deterministic, no model: GLB scale / raster resize-pad


class Intent(StrEnum):
    subtle = "subtle"
    related = "related"
    exploratory = "exploratory"


class Enforcement(StrEnum):
    machine_enforced = "machine_enforced"
    advisory_visual = "advisory_visual"
    unsupported = "unsupported"


METHOD_LABELS = {Method.image_edit_reconstruct: "Structural reconstruction", Method.image_edit: "Design variant",
                 Method.direct_transform: "Direct size transform"}
INTENT_NOTES = {Intent.subtle: "Small changes. The family resemblance stays strong.",
                Intent.related: "Clear differences with the same identity. Default.",
                Intent.exploratory: "Larger departures. Identity may drift; QA flags low resemblance."}

GENERATIVE_KINDS = (Kind.model3d, Kind.concept_art, Kind.sprite, Kind.icon)
DIRECT_KINDS = (Kind.model3d, Kind.concept_art, Kind.sprite, Kind.icon)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _finite_positive(v: float, what: str, lo: float = MIN_SCALE, hi: float = MAX_SCALE) -> float:
    if isinstance(v, bool) or not math.isfinite(v) or not lo <= v <= hi:
        raise ValueError(f"{what} must be a finite number within {lo:g}–{hi:g}")
    return float(v)


Anchor = Literal["source_origin", "bounds_center", "bottom_center"]


class UniformScale(Strict):
    op: Literal["uniform_scale"] = "uniform_scale"
    factor: float
    anchor: Anchor = "source_origin"

    @model_validator(mode="after")
    def _check(self) -> UniformScale:
        _finite_positive(self.factor, "factor")
        return self


class AxisScale(Strict):
    """Stretch/squash of the whole asset (not a structural redesign)."""
    op: Literal["axis_scale"] = "axis_scale"
    x: float
    y: float
    z: float
    anchor: Anchor = "source_origin"

    @model_validator(mode="after")
    def _check(self) -> AxisScale:
        for axis in ("x", "y", "z"):
            _finite_positive(getattr(self, axis), f"{axis} scale")
        return self


class TargetHeight(Strict):
    """Uniform scale so the world-space Y extent equals height_m. Needs a confirmed unit convention."""
    op: Literal["target_height"] = "target_height"
    height_m: float
    anchor: Anchor = "source_origin"
    units_confirmed: bool = False  # user confirmed 1 scene unit = 1 m (glTF convention) for this source

    @model_validator(mode="after")
    def _check(self) -> TargetHeight:
        _finite_positive(self.height_m, "height_m", 1e-4, MAX_TARGET_HEIGHT_M)
        return self


GlbTransform = Annotated[UniformScale | AxisScale | TargetHeight, Field(discriminator="op")]


class ResizeKeepAspect(Strict):
    op: Literal["resize_keep_aspect"] = "resize_keep_aspect"
    max_width: int = Field(ge=1, le=RASTER_MAX_SIDE)
    max_height: int = Field(ge=1, le=RASTER_MAX_SIDE)
    resample: Literal["lanczos", "nearest"] = "lanczos"


class PadCanvas(Strict):
    op: Literal["pad_canvas"] = "pad_canvas"
    width: int = Field(ge=1, le=RASTER_MAX_SIDE)
    height: int = Field(ge=1, le=RASTER_MAX_SIDE)
    placement: Literal["center", "bottom_center", "top_left"] = "center"
    background: Literal["transparent", "source_edge"] | str = "transparent"  # or #rrggbb

    @model_validator(mode="after")
    def _check(self) -> PadCanvas:
        bg = self.background
        if bg not in ("transparent", "source_edge") and not (len(bg) == 7 and bg[0] == "#" and all(
                c in "0123456789abcdefABCDEF" for c in bg[1:])):
            raise ValueError("background must be transparent, source_edge or #rrggbb")
        return self


RasterTransform = Annotated[ResizeKeepAspect | PadCanvas, Field(discriminator="op")]


class Constraint(Strict):
    id: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    text: str = Field(min_length=1, max_length=500)
    enforcement: Enforcement = Enforcement.advisory_visual


class VariantRow(Strict):
    """One intended new asset. `id` is stable across plan edits: seeds and bindings derive from it."""
    id: str = Field(pattern=r"^row_[0-9a-z]{6,24}$")
    label: str = Field(min_length=1, max_length=120)
    change_request: str = Field(default="", max_length=2000)
    final_height_m: float | None = None  # generative 3D: exact sizing applied AFTER reconstruction
    glb_transform: GlbTransform | None = None  # direct model3d
    raster_transform: RasterTransform | None = None  # direct 2D
    candidate_count: int = Field(default=DEFAULT_CANDIDATES, ge=1, le=MAX_CANDIDATES)
    confirm_duplicate: bool = False  # explicit ack that a no-op/duplicate result should still be a new identity

    @model_validator(mode="after")
    def _check(self) -> VariantRow:
        if self.final_height_m is not None:
            _finite_positive(self.final_height_m, "final_height_m", 1e-4, MAX_TARGET_HEIGHT_M)
        if self.glb_transform is not None and self.raster_transform is not None:
            raise ValueError("a row has at most one transform")
        return self


class SourceArtifactRef(Strict):
    role: str
    artifact_id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    mime: str


class SourceBinding(Strict):
    """Exact source: never resolved through a mutable 'current' pointer after binding."""
    project_id: str
    asset_id: str
    version_id: str
    display_version: int
    version_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # canonical hash of the immutable version record
    kind: Kind
    origin: str
    display_name: str
    primary_role: str
    artifacts: tuple[SourceArtifactRef, ...]
    style_sha: str | None = None  # style recorded by the source's config snapshot (None: not recorded)
    licence: dict[str, Any] = {}

    def artifact(self, role: str) -> SourceArtifactRef | None:
        return next((a for a in self.artifacts if a.role == role), None)


class ReferenceImage(Strict):
    """One single-object conditioning/QA image derived from the source version (never a contact sheet)."""
    artifact_id: str
    sha256: str
    role: Literal["primary", "auxiliary"]
    view: str  # e.g. "three_quarter", "rear", "side", "image"
    params: dict[str, Any] = {}


class FamilyChoice(Strict):
    """Existing family of the source, or a new family (created at materialization, source becomes anchor)."""
    family_id: str | None = None
    new_name: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def _check(self) -> FamilyChoice:
        if (self.family_id is None) == (self.new_name is None):
            raise ValueError("give exactly one of family_id / new_name")
        return self


PLAN_FIELDS = ("method", "intent", "preserve", "rows", "family", "references")


class VariantPlan(Strict):
    """Immutable, content-addressed plan (id derives from its canonical content). Edits make a new plan."""
    schema_version: int = VARIANT_SCHEMA
    id: str
    draft_id: str
    project_id: str
    source: SourceBinding
    method: Method
    intent: Intent | None  # None for direct transforms
    output_kind: Kind
    preserve: tuple[Constraint, ...] = ()
    rows: tuple[VariantRow, ...] = Field(min_length=1, max_length=MAX_ROWS)
    family: FamilyChoice
    references: tuple[ReferenceImage, ...] = ()
    reference_set_id: str | None = None
    style: dict[str, Any] = {}  # {sha, source_sha, conflict: bool, acknowledged: bool, application}
    planner: dict[str, Any] = {}  # {mode: manual|suggested, analysis_id, raw_ref, human_reviewed}
    created_at: str
    sha256: str = ""


class VariantDraft(BaseModel):
    """Mutable wizard state, optimistic `revision`. Saving never runs inference."""
    model_config = ConfigDict(extra="forbid")
    schema_version: int = VARIANT_SCHEMA
    id: str
    project_id: str
    revision: int = 1
    source: SourceBinding
    method: Method
    intent: Intent | None = Intent.related
    preserve: list[Constraint] = []
    rows: list[VariantRow] = []
    family: FamilyChoice
    candidates_per_row: int = Field(default=DEFAULT_CANDIDATES, ge=1, le=MAX_CANDIDATES)
    request: str = Field(default="", max_length=4000)  # natural-language set description
    reference_set_id: str | None = None
    primary_view: str | None = None
    analysis_id: str | None = None
    suggestion: dict[str, Any] | None = None  # last suggested rows (never applied without a user action)
    style_ack: bool = False
    created_at: str
    updated_at: str
    materialized: dict[str, Any] | None = None  # {plan_id, job_ids, batch_id, family_id} once saved as Jobs


class VariantContext(Strict):
    """What a variant Job carries (Job.variant). The plan holds the full, hashed contract."""
    plan_id: str
    plan_sha256: str
    row_id: str
    family_id: str
    method: Method
    intent: Intent | None
    source_asset_id: str
    source_version_id: str
    source_display_version: int
    source_name: str
    change_request: str = ""
    final_height_m: float | None = None


class Derivation(Strict):
    """AssetVersion.derivation: immutable lineage of a variant version."""
    method: Method
    intent: Intent | None
    source: SourceBinding
    family_id_at_publication: str
    family_anchor: dict[str, Any] = {}
    plan_id: str
    plan_sha256: str
    row_id: str
    style: dict[str, Any] = {}
    transform: dict[str, Any] | None = None  # direct: requested/effective transform + measured bounds
    references: tuple[ReferenceImage, ...] = ()


# --- capabilities -------------------------------------------------------------------------------------------------
Reason = Literal["missing_source", "corrupt_source", "unsupported_source_features", "missing_models",
                 "blocked_licence", "unsupported_configuration", "engine_unavailable", "experimental",
                 "source_not_published"]


def static_capability(kind: Kind, method: Method) -> tuple[bool, str]:
    """Kind x method support independent of runtime state (models, engines, source content)."""
    if method is Method.image_edit_reconstruct:
        return (kind is Kind.model3d, "structural reconstruction applies to 3D models")
    if method is Method.image_edit:
        if kind is Kind.material:
            return (False, "material design variants need a verified map-scope route (Surface/Seamless); "
                           "not available in this release")
        if kind in (Kind.sprite_sheet, Kind.vfx_flipbook):
            return (False, "generative sequence variants are not supported (no temporal consistency)")
        return (kind in GENERATIVE_KINDS and kind is not Kind.model3d, "")
    if kind in (Kind.sprite_sheet, Kind.vfx_flipbook, Kind.material):
        return (False, f"direct transforms of {kind.value} bundles need their own map/atlas contract")
    return (kind in DIRECT_KINDS, "")


def validate_rows(method: Method, kind: Kind, rows: list[VariantRow] | tuple[VariantRow, ...]) -> list[dict[str, Any]]:
    """Deterministic hard errors (schema/range/method conflicts). Semantic conflicts are advisory, elsewhere."""
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        def err(msg: str, r: VariantRow = r) -> None:
            errors.append({"row_id": r.id, "label": r.label, "code": "conflicting_variant_requirements",
                           "message": msg})
        if r.id in seen:
            err("duplicate row id")
        seen.add(r.id)
        if method is Method.direct_transform:
            if kind is Kind.model3d and r.glb_transform is None:
                err("a direct 3D row needs a scale or target height")
            if kind is not Kind.model3d and r.raster_transform is None:
                err("a direct image row needs a resize or canvas setting")
            if r.glb_transform is not None and kind is not Kind.model3d:
                err("GLB transforms apply to 3D models only")
            if r.raster_transform is not None and kind is Kind.model3d:
                err("raster transforms apply to images only")
            if isinstance(r.glb_transform, TargetHeight) and not r.glb_transform.units_confirmed:
                err("confirm that the source uses meters (1 unit = 1 m) before requesting an exact height")
        else:
            if r.glb_transform is not None or r.raster_transform is not None:
                err("transforms belong to Direct size transform Jobs; split into a separate Job")
            if not r.change_request.strip():
                err("describe what should differ")
            if r.final_height_m is not None and kind is not Kind.model3d:
                err("final height applies to 3D models only")
    return errors


def row_candidates(method: Method, row: VariantRow) -> int:
    return 1 if method is Method.direct_transform else row.candidate_count


def work_summary(method: Method, rows: list[VariantRow] | tuple[VariantRow, ...]) -> dict[str, int]:
    if method is Method.direct_transform:
        return {"rows": len(rows), "image_edits": 0, "builds": len(rows), "transforms": len(rows)}
    return {"rows": len(rows), "image_edits": sum(r.candidate_count for r in rows), "builds": len(rows),
            "transforms": 0}
