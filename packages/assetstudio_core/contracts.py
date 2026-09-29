"""Artifact-role contracts per asset kind, shared by generated, imported, derived, compared and exported versions."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .kinds import Kind


@dataclass(frozen=True)
class RoleContract:
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()  # regexes for families such as icon_<size> or frame_<index>

    def allows(self, role: str) -> bool:
        return role in self.required or role in self.optional or any(re.fullmatch(p, role) for p in self.patterns)


_MATERIAL_MAPS = ("normal", "roughness", "metallic", "ao", "height")

ROLE_CONTRACTS: dict[Kind, RoleContract] = {
    Kind.model3d: RoleContract(("model",), ("preview", "meta")),
    Kind.concept_art: RoleContract(("image",), ("preview", "meta")),
    Kind.sprite: RoleContract(("image",), ("preview", "meta")),
    Kind.icon: RoleContract(("image",), ("preview", "meta"), (r"icon_\d{1,5}",)),
    Kind.material: RoleContract(("base_color",), ("preview", "meta", "preview_tiled", *_MATERIAL_MAPS)),
    Kind.sprite_sheet: RoleContract(("atlas", "meta"), ("preview",), (r"frame_\d{4}",)),
    Kind.vfx_flipbook: RoleContract(("atlas", "meta"), ("preview",), (r"frame_\d{4}",)),
}


def check_roles(kind: Kind, roles: set[str]) -> list[str]:
    """Problems with a version's role set (empty = conforms)."""
    c = ROLE_CONTRACTS[kind]
    problems = [f"missing required role {r!r}" for r in c.required if r not in roles]
    problems += [f"role {r!r} is not part of the {kind.value} contract" for r in sorted(roles) if not c.allows(r)]
    return problems
