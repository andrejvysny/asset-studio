"""Field effect report: every set field names a consumer or is reported unsupported."""
from __future__ import annotations

from typing import Any

from assetstudio_core.config import StudioConfig
from assetstudio_core.effects import config_warnings, field_effects
from assetstudio_core.inheritance import build_snapshot


def _cfg(**defaults: Any) -> StudioConfig:
    return StudioConfig.model_validate({
        "project": {"id": "p", "name": "P"},
        "defaults": {"kind": "model3d", **defaults},
        "styles": {"fantasy": {"guide": "painterly", "negative": "photo",
                               "palette": [{"hex": "#ff0000", "reserved": True}]}},
        "reference_sets": {"mood": {"mode": "prompt_guidance", "images": [{"artifact_id": "art_x"}]},
                           "cond": {"mode": "image_conditioning", "images": [{"artifact_id": "art_y"}]}},
        "qa_rulesets": {"model3d": {"rules": [{"id": "pal", "source": "image_metric", "metric": "palette_reserved"}]}},
    })


def _by(effects: list[Any], field: str) -> list[Any]:
    return [e for e in effects if e.field == field]


def test_style_fields_name_their_consumers() -> None:
    snap = build_snapshot(_cfg(style="fantasy"), None)
    fx = field_effects(snap)
    assert [e.classification for e in _by(fx, "style.guide")] == ["conditioning_only"]
    assert _by(fx, "style.negative")[0].classification == "conditioning_only"
    assert _by(fx, "style.palette")[0].classification == "applied"
    edit = field_effects(snap, mode="edit")
    assert _by(edit, "style.negative")[0].classification == "not_applicable"
    assert {e.consumer for e in _by(edit, "style.guide")} == {"prompt enhancer (VLM)", "variant compare QA"}
    assert _by(edit, "parameters.steps")[0].classification == "not_applicable"


def test_inert_fields_are_unsupported_not_applied() -> None:
    snap = build_snapshot(_cfg(build_profile="painted", budget={"triangles": {"max": 5000}, "size_px": {"max": 64}}),
                          None)
    fx = field_effects(snap)
    assert _by(fx, "build_profile")[0].classification == "unsupported"
    assert _by(fx, "budget.size_px")[0].classification == "unsupported"
    assert _by(fx, "budget.triangles")[0].classification == "applied"


def test_reference_set_routing_and_legacy() -> None:
    snap = build_snapshot(_cfg(reference_set="mood"), None)
    assert _by(field_effects(snap), "reference_set.mood")[0].classification == "conditioning_only"
    legacy = {k: v for k, v in snap.items() if k != "reference_routing"}
    assert _by(field_effects(legacy), "reference_set.mood")[0].classification == "unsupported"
    cond = build_snapshot(_cfg(reference_set="cond"), None)
    assert _by(field_effects(cond), "reference_set.cond")[0].classification == "unsupported"


def test_config_warnings_only_for_explicit_inert_values() -> None:
    cfg = _cfg(build_profile="painted", budget={"frames": {"max": 8}})
    warnings = config_warnings({"defaults": cfg.defaults.model_dump(mode="json")})
    assert {w["path"] for w in warnings} == {"defaults.build_profile", "defaults.budget.frames"}
    assert config_warnings({"defaults": _cfg().defaults.model_dump(mode="json")}) == []
