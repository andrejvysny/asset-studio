"""Request-side rules for build retry modes (execution semantics: coordinator/builds/modes.py)."""
from __future__ import annotations

from typing import Any

from assetstudio_core.config import GEOMETRY_KEYS, MATERIAL_KEYS, GeometryPolicy, MaterialPolicy
from assetstudio_core.domain import BuildRun
from assetstudio_core.recipes import Recipe, validate_parameters
from pydantic import ValidationError

from ..errors import ApiError

RECIPE_KEYS = ("triangles", "texture_size", "remesh", "pipeline_type")
REBUILD_KEYS = RECIPE_KEYS + GEOMETRY_KEYS + MATERIAL_KEYS  # build-profile keys override the snapshot's profile


def check(recipe: Recipe, mode: str, overrides: dict[str, Any]) -> None:
    """Refuse a mode the recipe cannot honour or overrides that do not belong to it."""
    if mode in ("resample", "rebuild") and recipe.build != "model3d":
        raise ApiError(422, "mode_unsupported", f"{recipe.label} builds do not support {mode}")
    if mode != "rebuild":
        if overrides:
            raise ApiError(422, "invalid_parameters", f"settings overrides belong to the rebuild mode, not {mode}")
        return
    if not overrides:
        raise ApiError(422, "invalid_parameters", "rebuild needs at least one changed setting")
    if bad := [k for k in overrides if k not in REBUILD_KEYS]:
        raise ApiError(422, "invalid_parameters", f"not changeable by rebuild: {bad}; allowed {list(REBUILD_KEYS)}")
    if errors := validate_parameters(recipe, {k: v for k, v in overrides.items() if k in RECIPE_KEYS}):
        raise ApiError(422, "invalid_parameters", "; ".join(f"{k}: {m}" for k, m in errors))
    try:
        GeometryPolicy.model_validate({k: v for k, v in overrides.items() if k in GEOMETRY_KEYS})
        MaterialPolicy.model_validate({k: v for k, v in overrides.items() if k in MATERIAL_KEYS})
    except ValidationError as e:
        raise ApiError(422, "invalid_parameters", "; ".join(
            f"{'.'.join(str(x) for x in err['loc']) or 'material'}: {err['msg']}" for err in e.errors())) from e


def history_extra(run: BuildRun) -> dict[str, Any]:
    """How a run was requested, for the build history rows."""
    sample = run.checkpoints.get("sample")
    seed = run.inputs.get("seed_override")
    if seed is None and sample is not None:
        seed = sample.settings.get("seed")
    return {"mode": run.inputs.get("mode") or ("reexport" if run.kind == "reexport" else "build"), "seed": seed,
            "overrides": run.inputs.get("overrides") or {}}
