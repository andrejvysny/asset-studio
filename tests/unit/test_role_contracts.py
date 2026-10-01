"""Role contracts: model3d gained optional integration roles without loosening anything else."""
from __future__ import annotations

from assetstudio_core.contracts import check_roles
from assetstudio_core.kinds import Kind


def test_model3d_legacy_role_sets_still_validate() -> None:
    assert check_roles(Kind.model3d, {"model"}) == []
    assert check_roles(Kind.model3d, {"model", "preview", "meta"}) == []


def test_model3d_integration_roles_validate_and_stray_roles_fail() -> None:
    full = {"model", "preview", "descriptor", "godot_source", "conversion_report"}
    assert check_roles(Kind.model3d, full) == []
    assert check_roles(Kind.model3d, {"descriptor"}) == ["missing required role 'model'"]
    assert check_roles(Kind.model3d, {"model", "stray"}) == ["role 'stray' is not part of the model3d contract"]
    assert check_roles(Kind.sprite, {"image", "descriptor"}) == ["role 'descriptor' is not part of the sprite contract"]
