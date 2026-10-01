"""Godot integration contract v1 typed documents (INT-SPEC-1.0 §4): asset reference, descriptor, delivery manifest.

The JSON Schemas in contracts/godot-integration/v1 are the cross-language freeze. These models add the semantic checks
a schema cannot express (ordering, uniqueness, key recomputation) and must accept exactly what the schemas accept
plus reject those semantic violations. Raw bytes are what clients hash; only the writers below produce them.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Annotated, Any, Literal, get_args

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from . import canonical_v1

SERVER_ID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
LIBRARY_ID_PATTERN = r"^prj_[0-9a-hjkmnp-tv-z]{16}$"
ASSET_ID_PATTERN = r"^ast_[0-9a-hjkmnp-tv-z]{16}$"
VERSION_ID_PATTERN = r"^ver_[0-9a-hjkmnp-tv-z]{16}$"
ARTIFACT_ID_PATTERN = r"^art_[0-9a-hjkmnp-tv-z]{16}$"
DELIVERY_ID_PATTERN = r"^dlv_[0-9a-hjkmnp-tv-z]{16}$"
SLUG_PATTERN = r"^[a-z0-9][a-z0-9_.-]{0,63}$"
VERSION_PATTERN = r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
MEDIA_TYPE_PATTERN = r"^[a-z0-9]+/[a-z0-9.+-]+$"
# Segments of [A-Za-z0-9_.-], no "." / ".." segment, at most 32 segments (the {0,31} tail), 255 chars (maxLength).
SAFE_PATH_PATTERN = (
    r"^(?!\.{1,2}(?:/|$))[A-Za-z0-9_.-]+(?:/(?!\.{1,2}(?:/|$))[A-Za-z0-9_.-]+){0,31}$")
NODE_PATH_PATTERN = r"^(?:\.|[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*)$"

Representation = Literal["portable_glb_v1", "godot_static_source_v1", "mobile_glb_v1"]
Grounding = Literal["FOLLOW_TERRAIN", "WORLD_FIXED"]
Capability = Literal[
    "godot_text_scene_v1", "csg_static", "static_collision", "shader_source", "vertex_colors", "alpha_mask",
    "alpha_blend", "pbr_textures"]
KNOWN_CAPABILITIES: tuple[str, ...] = get_args(Capability)
IntegrationErrorCode = Literal[
    "unauthorized", "forbidden", "server_identity_mismatch", "unsupported_contract", "asset_not_found",
    "version_unavailable", "delivery_preparing", "unsupported_representation", "integrity_mismatch", "unsafe_package",
    "unsupported_source_dependency", "resource_limit", "stale_pointer", "idempotency_conflict", "cancelled",
    "temporarily_unavailable", "invalid_request", "preview_expired"]
INTEGRATION_ERROR_CODES: tuple[str, ...] = get_args(IntegrationErrorCode)
MAX_FILES = 4096
MAX_FILE_BYTES = 1073741824


def _pat(pattern: str, max_length: int | None = None) -> Any:
    return Annotated[str, StringConstraints(pattern=pattern, max_length=max_length)]


ServerId = _pat(SERVER_ID_PATTERN)
LibraryId = _pat(LIBRARY_ID_PATTERN)
AssetId = _pat(ASSET_ID_PATTERN)
VersionId = _pat(VERSION_ID_PATTERN)
ArtifactId = _pat(ARTIFACT_ID_PATTERN)
DeliveryId = _pat(DELIVERY_ID_PATTERN)
Slug = _pat(SLUG_PATTERN)
VersionText = _pat(VERSION_PATTERN)
Sha256Hex = _pat(SHA256_PATTERN)
MediaType = _pat(MEDIA_TYPE_PATTERN, 127)
SafePath = _pat(SAFE_PATH_PATTERN, 255)
NodePath = _pat(NODE_PATH_PATTERN, 1024)


def _decimal(text: str) -> Decimal:
    canonical_v1.parse_decimal(text)  # raises EncodingError (a ValueError) for anything non-canonical
    return Decimal(text)


def _check_decimal(text: str) -> str:
    _decimal(text)
    return text


def _check_positive(text: str) -> str:
    if _decimal(text) <= 0:
        raise ValueError(f"must be > 0: {text!r}")
    return text


Dec = Annotated[str, AfterValidator(_check_decimal)]
PosDec = Annotated[str, AfterValidator(_check_positive)]
Decimal3 = tuple[Dec, Dec, Dec]
DecimalRange = tuple[Dec, Dec]


class Frozen(BaseModel):
    # python-re: SAFE_PATH_PATTERN uses lookahead, which pydantic's default Rust engine does not support.
    model_config = ConfigDict(extra="forbid", frozen=True, regex_engine="python-re")


def unique_casefold(paths: list[str], label: str) -> None:
    folded = [p.casefold() for p in paths]
    if len(set(folded)) != len(folded):
        raise ValueError(f"{label}: duplicate paths (case-fold collisions count)")


def unique_values(values: list[str], label: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{label}: duplicate entries")


class AssetRef(Frozen):
    server_id: ServerId
    library_id: LibraryId
    asset_id: AssetId
    version_id: VersionId

    def key(self) -> str:
        return canonical_v1.asset_key(self.server_id, self.library_id, self.asset_id, self.version_id)


class PortableSurface(Frozen):
    mesh: int = Field(ge=0)
    primitive: int = Field(ge=0)


class SourceSurface(Frozen):
    node_path: NodePath
    surface: int = Field(ge=0)


SurfaceList = Annotated[list[PortableSurface | SourceSurface], Field(min_length=1, max_length=1024)]


class MaterialSlot(Frozen):
    slot_id: Slug
    role: Slug  # recommended: unspecified, surface, foliage, bark, trunk, stone, metal, glass, emissive
    surfaces: dict[Representation, SurfaceList]

    @model_validator(mode="after")
    def _surfaces_match_representation(self) -> MaterialSlot:
        if not self.surfaces:
            raise ValueError("surfaces needs at least one representation")
        for rep, items in self.surfaces.items():
            want = SourceSurface if rep == "godot_static_source_v1" else PortableSurface
            if not all(isinstance(i, want) for i in items):
                raise ValueError(f"surfaces[{rep}] entries must be {want.__name__}")
        return self


class CollisionInfo(Frozen):
    source: Literal["godot_static_source_v1"]
    shape_count: int = Field(ge=1, le=1024)
    shape_types: Annotated[
        list[Literal["box", "sphere", "capsule", "cylinder", "convex", "concave"]], Field(min_length=1, max_length=6)]

    @model_validator(mode="after")
    def _unique_types(self) -> CollisionInfo:
        unique_values(list(self.shape_types), "shape_types")
        return self


def check_no_floats(value: dict[str, Any]) -> dict[str, Any]:
    canonical_v1.canonical_bytes(value)  # EncodingError on floats / non-JSON values
    return value


def check_scale_and_height(scale: tuple[str, str], height: tuple[str, str]) -> None:
    if not 0 < _decimal(scale[0]) <= _decimal(scale[1]):
        raise ValueError("scale_range must satisfy 0 < min <= max")
    if _decimal(height[0]) > _decimal(height[1]):
        raise ValueError("height_offset_range_m must satisfy min <= max")


class PlacementFields(Frozen):
    """Placement metadata shared by the descriptor and the source-package manifest."""

    placement_anchor: Decimal3
    footprint_radius_m: PosDec
    scale_range: DecimalRange
    height_offset_range_m: DecimalRange
    default_grounding: Grounding

    @model_validator(mode="after")
    def _ranges(self) -> PlacementFields:
        check_scale_and_height(self.scale_range, self.height_offset_range_m)
        return self


class AssetDescriptorV1(PlacementFields):
    schema_version: Literal[1]
    asset_ref: AssetRef
    kind: Literal["model3d"]
    units: Literal["m"]
    up_axis: Literal["+Y"]
    forward_axis: Literal["+Z"]  # deviation from INT-SPEC §4.2 "-Z": see docs/adr/0001
    bounds_min: Decimal3
    bounds_max: Decimal3
    material_slots: Annotated[list[MaterialSlot], Field(max_length=64)]
    collision: CollisionInfo | None
    preview_warnings: Annotated[list[Slug], Field(max_length=64)]
    source_provenance: dict[str, Any]
    licence: dict[str, Any]

    @model_validator(mode="after")
    def _semantics(self) -> AssetDescriptorV1:
        extents = [_decimal(hi) - _decimal(lo) for lo, hi in zip(self.bounds_min, self.bounds_max, strict=True)]
        if any(e < 0 for e in extents):
            raise ValueError("bounds_min must be <= bounds_max on every axis")
        if not any(e > 0 for e in extents):
            raise ValueError("bounds must have a positive extent on at least one axis")
        unique_values([s.slot_id for s in self.material_slots], "material_slots.slot_id")
        check_no_floats(self.source_provenance)
        check_no_floats(self.licence)
        return self


class DeliveryFile(Frozen):
    path: SafePath
    sha256: Sha256Hex
    size: int = Field(ge=0, le=MAX_FILE_BYTES)
    media_type: MediaType
    artifact_id: ArtifactId


class DeliveryDependency(Frozen):
    asset_key: Sha256Hex
    asset_ref: AssetRef
    descriptor_sha256: Sha256Hex
    representation: Representation
    delivery_id: DeliveryId
    manifest_sha256: Sha256Hex

    @model_validator(mode="after")
    def _key(self) -> DeliveryDependency:
        if self.asset_key != self.asset_ref.key():
            raise ValueError("asset_key does not match asset_ref")
        return self


class Preparer(Frozen):
    name: Slug
    version: VersionText


class DeliveryManifestV1(Frozen):
    schema_version: Literal[1]
    delivery_id: DeliveryId
    asset_ref: AssetRef
    descriptor_sha256: Sha256Hex
    representation: Representation
    profile_id: Slug
    profile_version: Slug
    preparer: Preparer
    entrypoint: SafePath
    files: Annotated[list[DeliveryFile], Field(min_length=1, max_length=MAX_FILES)]
    dependencies: Annotated[list[DeliveryDependency], Field(max_length=MAX_FILES)]
    required_capabilities: list[Capability]

    @model_validator(mode="after")
    def _semantics(self) -> DeliveryManifestV1:
        paths = [f.path for f in self.files]
        unique_casefold(paths, "files")
        if self.entrypoint not in paths:
            raise ValueError("entrypoint must be one of files[].path")
        unique_values([d.asset_key for d in self.dependencies], "dependencies.asset_key")
        unique_values(list(self.required_capabilities), "required_capabilities")
        return self


def strict_loads(raw: bytes) -> Any:
    """json.loads that rejects float literals, NaN/Infinity and duplicate keys (contract documents carry none)."""

    def no_float(text: str) -> Any:
        raise ValueError(f"float literal {text!r} not allowed; non-integral values are decimal strings")

    def no_constant(name: str) -> Any:
        raise ValueError(f"{name} not allowed")

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out = dict(pairs)
        if len(out) != len(pairs):
            raise ValueError("duplicate object key")
        return out

    return json.loads(raw, parse_float=no_float, parse_constant=no_constant, object_pairs_hook=no_duplicates)


def document_bytes(model: BaseModel) -> bytes:
    return canonical_v1.canonical_bytes(model.model_dump(mode="json"))


def descriptor_bytes(d: AssetDescriptorV1) -> bytes:
    return document_bytes(d)


def manifest_bytes(m: DeliveryManifestV1) -> bytes:
    return document_bytes(m)


def parse_descriptor(raw: bytes) -> AssetDescriptorV1:
    return AssetDescriptorV1.model_validate(strict_loads(raw))


def parse_manifest(raw: bytes) -> DeliveryManifestV1:
    return DeliveryManifestV1.model_validate(strict_loads(raw))
