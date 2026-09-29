"""Neutral starter configuration for a new project. Technical checks only; no style, palette or categories."""
from __future__ import annotations

from .config import StudioConfig, empty_config
from .qa import QaRule, QaRuleset


def _vlm(rid: str, q: str, sev: str = "major") -> QaRule:
    return QaRule(id=rid, source="vlm", severity=sev, question=q)  # type: ignore[arg-type]


def _mask(rid: str, metric: str, sev: str = "major") -> QaRule:
    return QaRule(id=rid, source="mask_metric", metric=metric, severity=sev)  # type: ignore[arg-type]


def starter_rulesets() -> dict[str, QaRuleset]:
    no_text = _vlm("no_text", "The image contains no text, letters, logo or watermark.")
    return {
        "model3d": QaRuleset(label="3D model candidates", kind="model3d", rules=[
            _vlm("single_object", "Exactly one object is depicted."),
            _vlm("fully_visible", "The whole object is visible; nothing is cut off by the frame."),
            _vlm("no_environment", "The background is plain, with no scene or environment."),
            _vlm("no_floor", "There is no floor, ground plane or pedestal.", "minor"),
            _vlm("no_shadow", "There is no cast shadow.", "minor"),
            no_text,
            _vlm("suitable_3d", "The object is suitable for single-image 3D reconstruction."),
            _mask("mask_margin", "mask_margin"),
            _mask("mask_fill", "mask_fill", "minor"),
            _mask("mask_single_blob", "mask_single_blob"),
        ]),
        "sprite": QaRuleset(label="Sprite candidates", kind="sprite", rules=[
            _vlm("single_subject", "Exactly one subject is depicted."),
            _vlm("fully_visible", "The whole subject is visible; nothing is cut off."),
            no_text,
            _mask("mask_margin", "mask_margin"),
            _mask("mask_single_blob", "mask_single_blob", "minor"),
        ]),
        "icon": QaRuleset(label="Icon candidates", kind="icon", rules=[
            _vlm("centred", "The subject is centred and fits inside the square."),
            _vlm("single_shape", "The icon shows one clear, readable shape.", "minor"),
            no_text,
        ]),
        "concept_art": QaRuleset(label="Concept art candidates", kind="concept_art", rules=[
            _vlm("no_text", "The image contains no text, letters, logo or watermark.", "minor"),
        ]),
        "material": QaRuleset(label="Material candidates", kind="material", rules=[
            _vlm("surface_only", "The image shows only a surface texture, with no distinct objects."),
            _vlm("even_light", "Lighting is even, with no strong directional shadows.", "minor"),
        ]),
    }


def new_project_config(project_id: str, name: str, starter_qa: bool = True) -> StudioConfig:
    cfg = empty_config(project_id, name)
    if starter_qa:
        cfg.qa_rulesets = starter_rulesets()
    return cfg
