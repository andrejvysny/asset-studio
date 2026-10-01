"""Descriptor draft (INT-SPEC §5.2 preview upload): the placement fields a publisher declares.

The server never trusts a draft's geometry claims: bounds, asset reference and provenance are server-computed when the
draft becomes an `AssetDescriptorV1`. Schema: contracts/godot-integration/v1/publication-descriptor-draft.schema.json.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .delivery import (
    CollisionInfo,
    MaterialSlot,
    PlacementFields,
    Slug,
    document_bytes,
    strict_loads,
    unique_values,
)

DRAFT_REPRESENTATIONS = ("portable_glb_v1", "godot_static_source_v1")


class DraftSlot(MaterialSlot):
    """A material slot whose portable surfaces are mandatory (the GLB is always published)."""

    @model_validator(mode="after")
    def _portable_required(self) -> DraftSlot:
        if "portable_glb_v1" not in self.surfaces:
            raise ValueError("every slot needs portable_glb_v1 surfaces")
        extra = set(self.surfaces) - set(DRAFT_REPRESENTATIONS)
        if extra:
            raise ValueError(f"unsupported representations in a draft: {sorted(extra)}")
        return self


class DescriptorDraftV1(PlacementFields):
    schema_version: Literal[1]
    material_slots: Annotated[list[DraftSlot], Field(max_length=64)]
    collision: CollisionInfo | None
    preview_warnings: Annotated[list[Slug], Field(max_length=64)]

    @model_validator(mode="after")
    def _unique_slots(self) -> DescriptorDraftV1:
        unique_values([s.slot_id for s in self.material_slots], "material_slots.slot_id")
        return self


def parse_draft(raw: bytes) -> DescriptorDraftV1:
    return DescriptorDraftV1.model_validate(strict_loads(raw))


def draft_bytes(draft: DescriptorDraftV1) -> bytes:
    """Server-canonical bytes: what `descriptor_draft_sha256` hashes."""
    return document_bytes(draft)

