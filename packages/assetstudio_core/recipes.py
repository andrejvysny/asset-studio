"""Built-in recipe families: fixed stages + typed editable parameters. Projects select values, never graphs."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .kinds import Kind

ParamType = Literal["int", "float", "choice", "str", "bool", "int_list"]


@dataclass(frozen=True)
class ParamSpec:
    key: str
    type: ParamType
    default: Any
    note: str = ""
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] = ()

    def validate(self, value: Any) -> str | None:
        if self.type == "int_list":
            return self._validate_list(value)
        if self.type == "bool" and not isinstance(value, bool):
            return "must be true or false"
        if self.type == "int" and (isinstance(value, bool) or not isinstance(value, int)):
            return "must be an integer"
        if self.type == "float" and (isinstance(value, bool) or not isinstance(value, int | float)):
            return "must be a number"
        if self.type in ("str", "choice") and not isinstance(value, str):
            return "must be a string"
        if self.type == "choice" and value not in self.choices:
            return f"must be one of {list(self.choices)}"
        if self.min is not None and isinstance(value, int | float) and value < self.min:
            return f"must be >= {self.min:g}"
        if self.max is not None and isinstance(value, int | float) and value > self.max:
            return f"must be <= {self.max:g}"
        return None

    def _validate_list(self, value: Any) -> str | None:
        if not isinstance(value, list | tuple) or not 1 <= len(value) <= 8:
            return "must be a list of 1–8 integers"
        for v in value:
            if isinstance(v, bool) or not isinstance(v, int):
                return "must be a list of integers"
            if (self.min is not None and v < self.min) or (self.max is not None and v > self.max):
                return f"each value must be within {self.min:g}–{self.max:g}"
        return None


@dataclass(frozen=True)
class Stage:
    tag: Literal["prompt", "gate", "images", "qa", "build"]
    name: str
    backend: str


@dataclass(frozen=True)
class Recipe:
    id: str
    kind: Kind
    version: int
    label: str
    stages: tuple[Stage, ...]
    params: tuple[ParamSpec, ...]
    template: str  # neutral technical constraints appended after the enhanced description
    negative: str
    generation_models: tuple[str, ...]  # model-lock keys needed for candidates
    build_models: tuple[str, ...]
    generation: str | None  # adapter id, or None when no verified generation route exists
    generation_blocked_reason: str = ""
    build: str | None = None  # build implementation id, or None
    build_blocked_reason: str = ""
    qa_models: tuple[str, ...] = ("qwen3_vl_8b_instruct", "birefnet")
    candidate_mask: bool = True  # compute a foreground mask for candidate QA
    default_naming: str = "{name}"
    extra: dict[str, Any] = field(default_factory=dict)

    def param(self, key: str) -> ParamSpec | None:
        return next((p for p in self.params if p.key == key), None)

    def default_params(self) -> dict[str, Any]:
        return {p.key: p.default for p in self.params}


_T2I_MODELS = ("qwen_image_2512",)
_TEXT = Stage("prompt", "Enhance prompt", "local text model · GPU1")
_CONFIRM = Stage("gate", "Confirm prompts", "human")
_APPROVE = Stage("gate", "Approve", "human")
_PUBLISH = Stage("gate", "Accept + publish", "human")
_QA = Stage("qa", "QA", "local VLM + mask metrics · GPU1")


def _image_params(extra: tuple[ParamSpec, ...] = ()) -> tuple[ParamSpec, ...]:
    return (
        ParamSpec("candidate_count", "int", 4, "per item, 1–8", 1, 8),
        ParamSpec("steps", "int", 50, "image model sampling steps", 1, 150),
        ParamSpec("cfg", "float", 4.0, "classifier-free guidance", 0.0, 20.0),
        ParamSpec("width", "int", 1328, "candidate width px", 256, 2048),
        ParamSpec("height", "int", 1328, "candidate height px", 256, 2048),
        ParamSpec("speed_preset", "choice", "quality", "Lightning LoRA presets need the optional speed LoRA",
                  choices=("quality", "lightning_8step", "lightning_4step")),
    ) + extra


_NO_TEMPORAL = "no verified local temporal/pose generation adapter; import an ordered frame sequence instead"
_IMPORT_ONLY = "built from imported frame sequences (Import → frame sequence), not from batches"


def _atlas_params(extra: tuple[ParamSpec, ...] = ()) -> tuple[ParamSpec, ...]:
    return (
        ParamSpec("fps", "int", 12, "playback rate stored in the atlas metadata", 1, 120),
        ParamSpec("columns", "int", 0, "atlas grid columns; 0 = near-square", 0, 256),
        ParamSpec("padding", "int", 2, "px between frames", 0, 64),
        ParamSpec("pow2", "bool", False, "round atlas sides up to powers of two"),
    ) + extra

RECIPES: dict[str, Recipe] = {r.id: r for r in (
    Recipe(
        "model3d.default", Kind.model3d, 1, "3D model",
        (_TEXT, _CONFIRM, Stage("images", "Candidates", "ComfyUI · Qwen-Image · GPU0"), _QA, _APPROVE,
         Stage("build", "Cut-out", "BiRefNet · GPU1"), Stage("build", "Image → mesh", "TRELLIS.2 + DINOv3 · GPU1"),
         Stage("build", "Export GLB + validate", "worker3d exporter · GPU1, checks · CPU"), _PUBLISH),
        _image_params((
            ParamSpec("pipeline_type", "choice", "1024_cascade", "TRELLIS.2 resolution path",
                      choices=("512", "1024", "1024_cascade", "1536_cascade")),
            ParamSpec("triangles", "int", 100_000, "decimation target when the category sets no triangle budget",
                      1_000, 2_000_000),
            ParamSpec("texture_size", "int", 2048, "baked base colour + metallic/roughness", 512, 8192),
            ParamSpec("remesh", "bool", False, "dual-contouring remesh before simplification (rebuilds topology)"),
            ParamSpec("exporter", "choice", "clean",
                      "clean = no NVIDIA non-commercial code; research = upstream nvdiffrast (evaluation only)",
                      choices=("clean", "research")),
        )),
        "single isolated object, centred with margin, plain light grey background, no floor, no cast shadow, "
        "three-quarter view, even studio lighting",
        "multiple objects, scene, floor, pedestal, cast shadow, text, watermark, cropped",
        _T2I_MODELS, ("birefnet", "trellis2", "trellis_image_large", "dinov3_vitl16"), "comfyui.qwen_t2i",
        build="model3d",
    ),
    Recipe(
        "icon.default", Kind.icon, 1, "Icon",
        (_TEXT, _CONFIRM, Stage("images", "Art", "ComfyUI · Qwen-Image · GPU0"), _QA, _APPROVE,
         Stage("build", "Resize + pad variants", "CPU"), _PUBLISH),
        _image_params((
            ParamSpec("sizes", "int_list", (256, 128, 64, 32), "square output sizes px", 8, 2048),
            ParamSpec("background", "choice", "transparent", "transparent = BiRefNet cut-out",
                      choices=("transparent", "keep")),
            ParamSpec("padding", "float", 0.06, "margin inside each icon (transparent only)", 0.0, 0.4),
        )),
        "single centred subject, square composition, plain background, no text or letters",
        "text, letters, watermark, multiple subjects", _T2I_MODELS, ("birefnet",), "comfyui.qwen_t2i",
        build="icon",
    ),
    Recipe(
        "sprite.default", Kind.sprite, 1, "Sprite",
        (_TEXT, _CONFIRM, Stage("images", "Candidates", "ComfyUI · Qwen-Image · GPU0"), _QA, _APPROVE,
         Stage("build", "Cut-out + canvas", "BiRefNet · CPU"), _PUBLISH),
        _image_params((
            ParamSpec("alpha_threshold", "int", 128, "alpha counted as the sprite for trimming", 1, 255),
            ParamSpec("padding", "float", 0.04, "transparent margin, fraction of the canvas", 0.0, 0.4),
            ParamSpec("pivot", "choice", "bottom_center", "anchor recorded in the sprite metadata",
                      choices=("bottom_center", "center")),
            ParamSpec("canvas", "int", 512, "square canvas px; 0 = trimmed size + padding", 0, 4096),
        )),
        "single subject, plain background, full figure visible",
        "multiple subjects, scene, text, watermark, cropped", _T2I_MODELS, ("birefnet",), "comfyui.qwen_t2i",
        build="sprite",
    ),
    Recipe(
        "concept.default", Kind.concept_art, 1, "Concept art",
        (_TEXT, _CONFIRM, Stage("images", "Candidates", "ComfyUI · Qwen-Image · GPU0"), _QA, _APPROVE,
         Stage("build", "Finalize original resolution", "CPU"), _PUBLISH),
        _image_params(),
        "", "watermark, signature, text", _T2I_MODELS, (), "comfyui.qwen_t2i",
        build="passthrough", candidate_mask=False,
    ),
    Recipe(
        "material.default", Kind.material, 1, "Material",
        (_TEXT, _CONFIRM, Stage("images", "Tileable base colour", "ComfyUI · Qwen-Image · GPU0"), _QA, _APPROVE,
         Stage("build", "PBR maps", "explicitly configured derivation"), _PUBLISH),
        _image_params((
            ParamSpec("seam_max_ratio", "float", 2.0,
                      "wrap-edge ΔE ÷ neighbour ΔE; above this the build is invalid", 1.0, 10.0),
            ParamSpec("require_square", "bool", True, "invalid unless width == height"),
        )),
        "seamless tileable surface texture, orthographic top-down, even diffuse lighting, no objects",
        "objects, perspective, directional shadows, text", _T2I_MODELS, (), "comfyui.qwen_t2i",
        build="material", candidate_mask=False,
        extra={"derived_maps": "not generated: no verified local PBR derivation; import a material bundle instead"},
    ),
    Recipe(
        "sheet.default", Kind.sprite_sheet, 1, "Sprite sheet",
        (_TEXT, _CONFIRM, Stage("images", "Pose keys", "not available"), _QA, _APPROVE,
         Stage("build", "Frames + sheet", "atlas packer · CPU"), _PUBLISH),
        _atlas_params((ParamSpec("pivot", "choice", "bottom_center", "", choices=("bottom_center", "center")),)),
        "", "", (), (), None, generation_blocked_reason=_NO_TEMPORAL,
        build=None, build_blocked_reason=_IMPORT_ONLY, candidate_mask=False,
    ),
    Recipe(
        "vfx.default", Kind.vfx_flipbook, 1, "VFX flipbook",
        (_TEXT, _CONFIRM, Stage("images", "Keyframes", "not available"), _QA, _APPROVE,
         Stage("build", "Frames + atlas", "atlas packer · CPU"), _PUBLISH),
        _atlas_params((ParamSpec("blend", "choice", "alpha", "", choices=("alpha", "additive")),)),
        "", "", (), (), None, generation_blocked_reason=_NO_TEMPORAL,
        build=None, build_blocked_reason=_IMPORT_ONLY, candidate_mask=False,
    ),
)}

DEFAULT_RECIPE_FOR_KIND: dict[Kind, str] = {r.kind: r.id for r in RECIPES.values()}


def validate_parameters(recipe: Recipe, params: dict[str, Any]) -> list[tuple[str, str]]:
    errors: list[tuple[str, str]] = []
    for key, value in params.items():
        spec = recipe.param(key)
        if spec is None:
            errors.append((key, f"unknown parameter for {recipe.id}"))
        elif (msg := spec.validate(value)) is not None:
            errors.append((key, msg))
    return errors
