"""Build profile validation, snapshot embedding and per-field effects."""
from __future__ import annotations

from typing import Any

import pytest
from assetstudio_core.config import GEOMETRY_KEYS, MATERIAL_KEYS, StudioConfig, parse_config
from assetstudio_core.effects import field_effects
from assetstudio_core.inheritance import build_snapshot

PROFILE = {"label": "Foliage", "geometry": {"small_components": "preserve", "fill_holes": "disabled"},
           "material": {"alpha_mode": "auto", "alpha_cutoff": 0.4, "roughness_min": 0.2, "roughness_max": 0.9}}


def _cfg(kind: str = "model3d", profiles: dict[str, Any] | None = None) -> StudioConfig:
    return StudioConfig.model_validate({
        "project": {"id": "p", "name": "P"},
        "build_profiles": {"foliage": PROFILE} if profiles is None else profiles,
        "defaults": {"kind": kind, "build_profile": {"mode": "value", "value": "foliage"}},
    })


def _errors(profiles: dict[str, Any]) -> list[str]:
    data: dict[str, Any] = {"project": {"id": "p", "name": "P"}, "build_profiles": profiles}
    cfg, errs = parse_config(data)
    return [e.message for e in errs] if cfg is None else []


def test_keys_are_exported() -> None:
    assert GEOMETRY_KEYS == ("small_components", "fill_holes")
    assert "roughness_max" in MATERIAL_KEYS


@pytest.mark.parametrize("material", [
    {"alpha_mode": "opaque", "alpha_cutoff": 0.5},
    {"alpha_mode": "blend", "alpha_cutoff": 0.5},
    {"roughness_min": 0.8, "roughness_max": 0.2},
    {"alpha_cutoff": 1.5},
    {"unknown": 1},
])
def test_invalid_material_rejected(material: dict[str, Any]) -> None:
    assert _errors({"x": {"material": material}})


def test_invalid_profile_id_rejected() -> None:
    assert _errors({"Bad Id!": {}})


def test_unknown_reference_is_field_error() -> None:
    from assetstudio_core.inheritance import validate_semantics  # noqa: PLC0415

    errs = validate_semantics(_cfg(profiles={}))
    assert any(e.message == "unknown build_profile 'foliage'" for e in errs)


def test_snapshot_embeds_profile_and_source() -> None:
    snap = build_snapshot(_cfg(), None)
    assert snap["build_profile"]["material"]["alpha_mode"] == "auto"
    assert snap["build_profile"]["geometry"]["fill_holes"] == "disabled"
    assert snap["sources"]["build_profile"] == "project"
    no_profile = build_snapshot(StudioConfig.model_validate(
        {"project": {"id": "p", "name": "P"}, "defaults": {"kind": "model3d"}}), None)
    assert no_profile["build_profile"] is None


def test_effects_applied_for_model3d_not_applicable_for_icon() -> None:
    fx = {e.field: e for e in field_effects(build_snapshot(_cfg(), None))}
    assert fx["build_profile.geometry.small_components"].classification == "applied"
    assert fx["build_profile.geometry.fill_holes"].consumer == "3D worker export cleanup"
    assert "not controlled" in fx["build_profile.geometry.fill_holes"].note
    assert fx["build_profile.material.alpha_mode"].consumer == "CPU material stage (GLB rewrite)"
    assert "1%" in fx["build_profile.material.alpha_mode"].note
    assert "build_profile.geometry.expect_single_component" not in fx
    icon = {e.field: e for e in field_effects(build_snapshot(_cfg("icon"), None))}
    assert icon["build_profile.material.alpha_cutoff"].classification == "not_applicable"
