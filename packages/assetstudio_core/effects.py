"""What each resolved setting of a snapshot is planned to do, by which consumer.

This is the planned mechanism, not evidence that anything ran: execution receipts and checks prove that. A field
with no consumer is reported `unsupported`, never silently treated as applied.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

Classification = Literal["applied", "conditioning_only", "advisory_only", "not_applicable", "unsupported"]
Mode = Literal["t2i", "edit"]

ROUTING_VERSION = 1  # snapshots carrying this `reference_routing` value route their project reference set
# Fields that exist in studio.yaml but have no consumer yet; a set value is reported, never silently accepted.
INERT_DEFAULTS = {
    "build_profile": "no build reads it yet; 3D export uses the recipe parameters",
    "export_presets": "no exporter reads it yet (files/Godot/git delivery is planned)",
}
INERT_BUDGET = {
    "size_px": "no build reads it yet",
    "frames": "no build reads it yet",
}


class Effect(BaseModel):
    field: str
    value: Any = None
    source: str = ""
    consumer: str
    classification: Classification
    note: str = ""


def _e(field: str, value: Any, source: str, consumer: str, cls: Classification, note: str = "") -> Effect:
    return Effect(field=field, value=value, source=source, consumer=consumer, classification=cls, note=note)


def _style(snap: dict[str, Any], mode: Mode) -> list[Effect]:
    style, src = snap.get("style") or {}, (snap.get("sources") or {}).get("style", "")
    if not style:
        return []
    kind = snap["recipe"]["kind"]
    out = []
    if style.get("guide"):
        out.append(_e("style.guide", style["guide"], src, "prompt enhancer (VLM)", "conditioning_only",
                      "guides the written description; the image model is not forced to comply"))
        if mode == "edit":
            out.append(_e("style.guide", style["guide"], src, "variant compare QA", "advisory_only",
                          "asked as the variant_style check; never blocks approval"))
    if style.get("negative"):
        out.append(_e("style.negative", style["negative"], src, "image model negative prompt",
                      "conditioning_only" if mode == "t2i" else "not_applicable",
                      "appended to the recipe negative" if mode == "t2i"
                      else "source-conditioned edits use fixed edit negatives"))
    chain = set(snap.get("category_chain") or [])  # same scope rule as coordinator qa.reserved_colours
    reserved = [c for c in style.get("palette") or [] if c.get("reserved") and kind not in c.get("allowed_kinds", [])
                and not chain & set(c.get("allowed_categories", []))]
    rules = (snap.get("qa_ruleset") or {}).get("rules") or []
    enabled = any(r.get("metric") == "palette_reserved" and r.get("enabled", True)
                  and r.get("stage", "candidate") == "candidate" for r in rules)
    if reserved:
        out.append(_e("style.palette", [c["hex"] for c in reserved], src, "candidate QA palette_reserved",
                      "applied" if enabled else "not_applicable",
                      "measured on candidate pixels" if enabled else "no enabled palette_reserved rule in the QA set"))
    elif style.get("palette"):
        out.append(_e("style.palette", [c["hex"] for c in style["palette"]], src, "none", "advisory_only",
                      "unreserved colours are reference information only"))
    return out


def _references(snap: dict[str, Any], mode: Mode) -> list[Effect]:
    rs = snap.get("reference_set")
    if not rs:
        return []
    set_id = (snap.get("values") or {}).get("reference_set")
    src = (snap.get("sources") or {}).get("reference_set", "")
    field = f"reference_set.{set_id}"
    count = f"{len(rs.get('images') or [])} image(s)"
    if snap.get("reference_routing") != ROUTING_VERSION:
        return [_e(field, count, src, "none", "unsupported",
                   "Job created before project reference sets were routed; attach images to the item instead")]
    if rs["mode"] == "prompt_guidance":
        return [_e(field, count, src, "prompt enhancer (VLM)", "conditioning_only",
                   "after item references, within the enhancer's 4-image limit"
                   + ("; the source image takes one slot" if mode == "edit" else ""))]
    if rs["mode"] == "qa_reference":
        return [_e(field, count, src, "candidate compare QA", "advisory_only",
                   "one resemblance check per reference, after item references (max 4)")]
    return [_e(field, count, src, "none", "unsupported",
               "image conditioning: no reference-capable image adapter; generation is blocked")]


def _values(snap: dict[str, Any], mode: Mode) -> list[Effect]:
    values, sources = snap.get("values") or {}, snap.get("sources") or {}
    kind = snap["recipe"]["kind"]
    out = [
        _e("recipe_id", values.get("recipe_id"), sources.get("recipe_id", ""), "stage plan", "applied"),
        _e("naming", values.get("naming"), sources.get("naming", ""), "publication file names", "applied"),
    ]
    if values.get("qa_ruleset"):
        out.append(_e("qa_ruleset", values["qa_ruleset"], sources.get("qa_ruleset", ""), "candidate QA", "applied"))
    if values.get("style_lora"):
        out.append(_e("style_lora", values["style_lora"], sources.get("style_lora", ""), "image model",
                      "unsupported", "style LoRAs are not available; generation is blocked"))
    for name, note in INERT_DEFAULTS.items():
        if values.get(name):
            out.append(_e(name, values[name], sources.get(name, ""), "none", "unsupported", note))
    budget = values.get("budget") or {}
    if budget.get("triangles"):
        out.append(_e("budget.triangles", budget["triangles"], sources.get("budget", ""),
                      "3D export decimation target + triangle_budget check" if kind == "model3d" else "none",
                      "applied" if kind == "model3d" else "not_applicable"))
    for name, note in INERT_BUDGET.items():
        if budget.get(name):
            out.append(_e(f"budget.{name}", budget[name], sources.get("budget", ""), "none", "unsupported", note))
    return out


def _parameters(snap: dict[str, Any], mode: Mode) -> list[Effect]:
    edit_ignored = {"steps", "cfg", "width", "height", "speed_preset"}
    sources = snap.get("parameter_sources") or {}
    out = []
    for key, value in (snap.get("parameters") or {}).items():
        skipped = mode == "edit" and key in edit_ignored
        out.append(_e(f"parameters.{key}", value, sources.get(key, "recipe"),
                      "variant edit plan" if skipped else f"recipe {snap['recipe']['id']}",
                      "not_applicable" if skipped else "applied",
                      "source-conditioned edits use the plan's edit parameters" if skipped else ""))
    return out


def field_effects(snap: dict[str, Any], mode: Mode = "t2i") -> list[Effect]:
    """Every resolved setting of `snap` with its consumer. `mode`: "edit" for source-conditioned variant Jobs."""
    return _values(snap, mode) + _parameters(snap, mode) + _style(snap, mode) + _references(snap, mode)


def config_warnings(values_by_scope: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    """Non-fatal: explicitly set fields that nothing reads. `values_by_scope`: {"defaults": {...}, "categories.x..."}.
    Inputs are CategoryDefaults dumps (Override dicts)."""
    out = []
    for scope, d in values_by_scope.items():
        for name, note in INERT_DEFAULTS.items():
            if (d.get(name) or {}).get("mode") == "value":
                out.append({"path": f"{scope}.{name}", "message": f"has no effect yet: {note}"})
        budget = d.get("budget") or {}
        if budget.get("mode") == "value":
            for name, note in INERT_BUDGET.items():
                if (budget.get("value") or {}).get(name):
                    out.append({"path": f"{scope}.budget.{name}", "message": f"has no effect yet: {note}"})
    return out
