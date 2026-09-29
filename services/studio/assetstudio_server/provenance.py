"""Licence/provenance capture for executions: only components that actually produced the artifact."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from assetstudio_core.safeyaml import load_yaml

SEVERITY = {"cleared": 0, "unknown": 1, "review": 2, "not_cleared": 3}


def load_licences(config_dir: Path) -> dict[str, dict[str, Any]]:
    return (load_yaml((config_dir / "licences.yaml").read_bytes()) or {}).get("components", {})


def licence_summary(config_dir: Path, models_used: list[str], generation: dict[str, Any],
                    build_components: list[str] | None = None) -> dict[str, Any]:
    table = load_licences(config_dir)
    ids = ["comfyui", *models_used] if generation.get("engine") == "comfyui" else list(models_used)
    ids += [c for c in build_components or [] if c not in ids]
    if generation.get("speed_lora"):
        ids.append("qwen_image_2512_lightning")
    comps = [{"id": i, **table.get(i, {"name": i, "licence": "unknown", "status": "unknown"})} for i in ids]
    if generation.get("style_lora"):
        comps.append({"id": "style_lora", "name": generation["style_lora"].get("file"), "licence": "unknown",
                      "status": "unknown"})
    if generation.get("simulated"):
        comps.append({"id": "simulation", "name": "SIMULATED engine output", "licence": "n/a",
                      "status": "not_cleared", "note": "test/demo output, not a real generation"})
    worst = max((c.get("status", "unknown") for c in comps), key=lambda s: SEVERITY.get(s, 1), default="unknown")
    return {"status": worst, "components": comps,
            "note": "Captured at creation. Traceability only; not a legal guarantee of rights or non-infringement."}


def _worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: SEVERITY.get(s, 1), default="unknown")


def add_build_components(licence: dict[str, Any], config_dir: Path,
                         build_components: list[str] | None) -> dict[str, Any]:
    """Extend a generation-time licence record with build components, resolved against today's table."""
    table = load_licences(config_dir)
    have = {c["id"] for c in licence["components"]}
    extra = [{"id": i, **table.get(i, {"name": i, "licence": "unknown", "status": "unknown"})}
             for i in dict.fromkeys(build_components or []) if i not in have]
    comps = [*licence["components"], *extra]
    return {**licence, "components": comps, "status": _worst([c.get("status", "unknown") for c in comps]),
            "note": licence.get("note", "") + " Generation components are as recorded at generation; build "
            "component statuses were resolved at publication."}


def derived_licence(processing: dict[str, Any], source_licence: dict[str, Any], derivation: str) -> dict[str, Any]:
    """A derived asset is never better licensed than its source: the worse of processing and source status wins."""
    p_status = processing.get("status", "unknown")
    s_status = source_licence.get("status") or "unknown"
    return {"status": _worst([p_status, s_status]), "processing_status": p_status, "source_status": s_status,
            "components": processing.get("components", []), "source_licence": source_licence,
            "derivation": derivation,
            "note": "Status is the more restrictive of the processing components and the source version's licence "
                    "record; a derivation never improves a source's unresolved status. "
                    "Traceability only; not a legal guarantee of rights or non-infringement."}
