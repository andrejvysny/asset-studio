"""studio.yaml schema. Explicit inherit/value/disabled overrides; no executable fields anywhere."""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .ids import validate_config_key
from .kinds import Kind
from .qa import QaRuleset

T = TypeVar("T")
SCHEMA_VERSION = 1


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Mode(StrEnum):
    inherit = "inherit"
    value = "value"
    disabled = "disabled"


class Override(Strict, Generic[T]):
    """Missing/None = inherit. `disabled` deliberately means none (e.g. no LoRA even if a parent sets one)."""

    mode: Mode = Mode.inherit
    value: T | None = None

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, data: Any) -> Any:
        if data is None or data == {}:
            return {"mode": "inherit"}
        if isinstance(data, dict) and "mode" in data:
            return data
        return {"mode": "value", "value": data}

    @model_validator(mode="after")
    def _consistent(self) -> Override[T]:
        if self.mode == Mode.value and self.value is None:
            raise ValueError("mode 'value' requires a value")
        if self.mode != Mode.value and self.value is not None:
            raise ValueError(f"mode {self.mode.value!r} must not carry a value")
        return self


class IntRange(Strict):
    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)
    advisory: bool = True

    @model_validator(mode="after")
    def _ordered(self) -> IntRange:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min must be <= max")
        return self


class Budget(Strict):
    triangles: IntRange | None = None
    size_px: IntRange | None = None
    frames: IntRange | None = None


class LoraRef(Strict):
    model_id: str
    strength: float = Field(ge=-2.0, le=2.0)


class CategoryDefaults(Strict):
    kind: Override[Kind] = Field(default_factory=Override[Kind])
    recipe_id: Override[str] = Field(default_factory=Override[str])
    naming: Override[str] = Field(default_factory=Override[str])
    budget: Override[Budget] = Field(default_factory=Override[Budget])
    build_profile: Override[str] = Field(default_factory=Override[str])
    qa_ruleset: Override[str] = Field(default_factory=Override[str])
    reference_set: Override[str] = Field(default_factory=Override[str])
    style: Override[str] = Field(default_factory=Override[str])
    style_lora: Override[LoraRef] = Field(default_factory=Override[LoraRef])
    export_presets: Override[list[str]] = Field(default_factory=Override[list[str]])
    candidate_count: Override[int] = Field(default_factory=Override[int])


DEFAULT_FIELDS: tuple[str, ...] = tuple(CategoryDefaults.model_fields)


class Category(Strict):
    id: str
    parent_id: str | None = None
    slug: str
    label: str = Field(min_length=1, max_length=120)
    archived: bool = False
    defaults: CategoryDefaults = CategoryDefaults()
    metadata: dict[str, str] = {}

    @field_validator("id", "slug")
    @classmethod
    def _key(cls, v: str) -> str:
        return validate_config_key(v)


class PipelineSettings(Strict):
    recipe_version: int = 1
    parameters: dict[str, Any] = {}
    template: str | None = Field(default=None, max_length=2000)


class PaletteColor(Strict):
    hex: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    label: str = ""
    reserved: bool = False
    # Kinds/categories where a reserved colour is permitted (e.g. icons). Empty = nowhere.
    allowed_kinds: list[Kind] = []
    allowed_categories: list[str] = []
    tolerance_delta_e: float = Field(default=8.0, gt=0, le=100)


class StyleProfile(Strict):
    label: str = ""
    guide: str = Field(default="", max_length=8000)
    negative: str = Field(default="", max_length=2000)
    palette: list[PaletteColor] = []


class ReferenceImage(Strict):
    artifact_id: str
    label: str = ""
    role: str = ""
    source_rights: str = "unknown"


class ReferenceSet(Strict):
    label: str = ""
    mode: Literal["prompt_guidance", "image_conditioning", "qa_reference"] = "prompt_guidance"
    images: list[ReferenceImage] = Field(default=[], max_length=4)


class ExportPreset(Strict):
    type: Literal["files", "godot", "git"]
    enabled: bool = False
    trigger: Literal["on_publish", "manual"] = "manual"
    destination_ref: str | None = None
    path_pattern: str = "{category}/{asset_id}/{filename}"
    settings: dict[str, Any] = {}


class RetentionRule(Strict):
    keep: bool = True
    expire_after_days: int | None = Field(default=None, ge=1)


class Retention(Strict):
    rejected_candidates: RetentionRule = RetentionRule()
    raw_intermediates: RetentionRule = RetentionRule()
    full_logs: RetentionRule = RetentionRule()


GEOMETRY_KEYS: tuple[str, ...] = ("small_components", "fill_holes")
MATERIAL_KEYS: tuple[str, ...] = (
    "alpha_mode", "alpha_cutoff", "double_sided", "metallic", "roughness_min", "roughness_max",
)


class GeometryPolicy(Strict):
    small_components: Literal["remove", "preserve"] | None = None  # None = exporter default (remove)
    fill_holes: Literal["upstream", "disabled"] | None = None  # None = exporter default (upstream)
    expect_single_component: bool | None = None  # None = advisory check as today


class MaterialPolicy(Strict):
    alpha_mode: Literal["opaque", "mask", "blend", "auto"] | None = None
    alpha_cutoff: float | None = Field(default=None, ge=0.0, le=1.0)
    double_sided: bool | None = None
    metallic: float | None = Field(default=None, ge=0.0, le=1.0)  # replace
    roughness_min: float | None = Field(default=None, ge=0.0, le=1.0)  # clamp
    roughness_max: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _consistent(self) -> MaterialPolicy:
        if None not in (self.roughness_min, self.roughness_max) and self.roughness_min > self.roughness_max:  # type: ignore[operator]
            raise ValueError("roughness_min must be <= roughness_max")
        if self.alpha_cutoff is not None and self.alpha_mode in ("opaque", "blend"):
            raise ValueError("alpha_cutoff only applies to alpha_mode mask or auto")
        return self


class BuildProfile(Strict):
    label: str = ""
    geometry: GeometryPolicy = GeometryPolicy()
    material: MaterialPolicy = MaterialPolicy()


class ProjectInfo(Strict):
    id: str
    name: str = Field(min_length=1, max_length=120)


class StudioConfig(Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    project: ProjectInfo
    revision: int = Field(default=1, ge=1)
    defaults: CategoryDefaults = CategoryDefaults()
    categories: list[Category] = []
    pipelines: dict[str, PipelineSettings] = {}
    qa_rulesets: dict[str, QaRuleset] = {}
    styles: dict[str, StyleProfile] = {}
    build_profiles: dict[str, BuildProfile] = {}
    reference_sets: dict[str, ReferenceSet] = {}
    export_presets: dict[str, ExportPreset] = {}
    retention: Retention = Retention()

    @field_validator("build_profiles")
    @classmethod
    def _profile_keys(cls, v: dict[str, BuildProfile]) -> dict[str, BuildProfile]:
        for key in v:
            validate_config_key(key)
        return v

    def category(self, category_id: str) -> Category | None:
        return next((c for c in self.categories if c.id == category_id), None)


class FieldError(BaseModel):
    path: str
    message: str


def _loc(loc: tuple[Any, ...]) -> str:
    return ".".join(str(p) for p in loc if p not in ("Override[Kind]",)) or "(root)"


def parse_config(data: Any) -> tuple[StudioConfig | None, list[FieldError]]:
    try:
        cfg = StudioConfig.model_validate(data)
    except ValidationError as e:
        return None, [FieldError(path=_loc(err["loc"]), message=err["msg"]) for err in e.errors()]
    return cfg, []


def empty_config(project_id: str, name: str) -> StudioConfig:
    return StudioConfig(project=ProjectInfo(id=project_id, name=name))
