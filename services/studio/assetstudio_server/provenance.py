"""Licence/provenance capture for executions: only components that actually produced the artifact."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from assetstudio_core.safeyaml import load_yaml

SEVERITY = {"cleared": 0, "unknown": 1, "review": 2, "not_cleared": 3}


def load_licences(config_dir: Path) -> dict[str, dict[str, Any]]:
    return (load_yaml((config_dir / "licences.yaml").read_bytes()) or {}).get("components", {})


def licence_summary(config_dir: Path, models_used: list[str], generation: dict[str, Any]) -> dict[str, Any]:
    table = load_licences(config_dir)
    ids = ["comfyui", *models_used] if generation.get("engine") == "comfyui" else list(models_used)
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
