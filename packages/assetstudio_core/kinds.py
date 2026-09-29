"""Asset kind registry. Origin (generated/imported/...) is provenance, never a kind."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Kind(StrEnum):
    model3d = "model3d"
    sprite = "sprite"
    icon = "icon"
    vfx_flipbook = "vfx_flipbook"
    material = "material"
    sprite_sheet = "sprite_sheet"
    concept_art = "concept_art"


class Origin(StrEnum):
    generated = "generated"
    imported = "imported"
    derived = "derived"
    mixed = "mixed"


@dataclass(frozen=True)
class KindInfo:
    label: str
    sub: str
    build_label: str
    three_d: bool


KINDS: dict[Kind, KindInfo] = {
    Kind.model3d: KindInfo("3D model", "image → cut-out → mesh → GLB", "3D", True),
    Kind.sprite: KindInfo("Sprite", "image → cut-out → PNG", "Cut-out", False),
    Kind.icon: KindInfo("Icon", "art → sized variants", "Compose", False),
    Kind.vfx_flipbook: KindInfo("VFX flipbook", "frames → atlas", "Frames", False),
    Kind.material: KindInfo("Material", "tileable → PBR maps", "PBR maps", False),
    Kind.sprite_sheet: KindInfo("Sprite sheet", "frames → sheet", "Frames", False),
    Kind.concept_art: KindInfo("Concept art", "image → final", "Finalize", False),
}
