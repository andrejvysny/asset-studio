"""`source_manifest.json` of GodotStaticSourcePackageV1 (INT-SPEC-1.0 §7/§8).

Grammar: contracts/godot-integration/v1/static-source-package.md."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from .delivery import (
    MAX_FILE_BYTES,
    MAX_FILES,
    AssetRef,
    Capability,
    DeliveryId,
    Frozen,
    MediaType,
    PlacementFields,
    SafePath,
    Sha256Hex,
    Slug,
    SourceSurface,
    document_bytes,
    strict_loads,
    unique_casefold,
    unique_values,
)

MANIFEST_NAME = "source_manifest.json"
# res:// keys: no "..", no backslash, no control characters. Dependency/entry paths are checked separately.
RES_PATH_PATTERN = r"^res://(?!(?:.*/)?\.\.(?:/|$))[^\\\x00-\x1f]+$"
UID_PATTERN = r"^uid://[a-z0-9]{1,32}$"
GODOT_VERSION_PATTERN = r"^[0-9]+\.[0-9]+(\.[0-9]+)?(-[0-9A-Za-z.]+)?$"
ResPath = Annotated[str, StringConstraints(pattern=RES_PATH_PATTERN, max_length=1024)]
Uid = Annotated[str, StringConstraints(pattern=UID_PATTERN)]
Text256 = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class SourceFile(Frozen):
    path: SafePath
    sha256: Sha256Hex
    size: int = Field(ge=0, le=MAX_FILE_BYTES)
    media_type: MediaType


class PackageFileEntry(Frozen):
    kind: Literal["package_file"]
    path: SafePath
    original_uid: Uid | None


class AssetDependencyEntry(Frozen):
    kind: Literal["asset_dependency"]
    asset_key: Sha256Hex
    entrypoint: SafePath
    original_uid: Uid | None


class SourceAssetDependency(Frozen):
    asset_ref: AssetRef
    descriptor_sha256: Sha256Hex
    representation: Literal["godot_static_source_v1", "portable_glb_v1"]
    delivery_id: DeliveryId | None


class Approximation(Frozen):
    slot_id: Slug
    reason: Text256


class ConversionReport(Frozen):
    portable_status: Literal["exact", "approximated", "desktop_only"]
    omissions: Annotated[list[Text256], Field(max_length=256)]
    approximations: Annotated[list[Approximation], Field(max_length=256)]


class PlacementSlot(Frozen):
    slot_id: Slug
    role: Slug
    source_surfaces: Annotated[list[SourceSurface], Field(min_length=1, max_length=1024)]


class SourcePlacement(PlacementFields):
    material_slots: Annotated[list[PlacementSlot], Field(max_length=64)]


class SourcePackageManifestV1(Frozen):
    schema_version: Literal[1]
    entry_scene: SafePath
    source_godot_version: Annotated[str, StringConstraints(pattern=GODOT_VERSION_PATTERN)]
    files: Annotated[list[SourceFile], Field(min_length=1, max_length=MAX_FILES)]
    resource_map: dict[ResPath, Annotated[PackageFileEntry | AssetDependencyEntry, Field(discriminator="kind")]]
    asset_dependencies: dict[Sha256Hex, SourceAssetDependency]
    capabilities: list[Capability]
    conversion_report: ConversionReport
    placement: SourcePlacement

    @model_validator(mode="after")
    def _semantics(self) -> SourcePackageManifestV1:
        paths = [f.path for f in self.files]
        unique_casefold(paths, "files")
        if MANIFEST_NAME in paths:
            raise ValueError(f"files must not list {MANIFEST_NAME}")
        if not self.entry_scene.endswith(".tscn") or self.entry_scene not in paths:
            raise ValueError("entry_scene must be a .tscn listed in files")
        unique_values(list(self.capabilities), "capabilities")
        for key, dep in self.asset_dependencies.items():
            if key != dep.asset_ref.key():
                raise ValueError(f"asset_dependencies key {key} does not match its asset_ref")
        self._check_resource_map(set(paths))
        return self

    def _check_resource_map(self, paths: set[str]) -> None:
        for res, entry in self.resource_map.items():
            if isinstance(entry, PackageFileEntry) and entry.path not in paths:
                raise ValueError(f"resource_map[{res}] names a file not in files: {entry.path}")
            if isinstance(entry, AssetDependencyEntry) and entry.asset_key not in self.asset_dependencies:
                raise ValueError(f"resource_map[{res}] names an unknown asset dependency")


def source_manifest_bytes(m: SourcePackageManifestV1) -> bytes:
    return document_bytes(m)


def parse_source_manifest(raw: bytes) -> SourcePackageManifestV1:
    return SourcePackageManifestV1.model_validate(strict_loads(raw))

