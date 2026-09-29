"""Stage descriptors, residency signatures and task construction.

Residency signature = what must be loaded to run a task (engine/backend + exact model identities + weight-changing
modifiers). It never contains prompts, seeds, categories, kinds or Job ids, so compatible work of different Jobs
shares one model residency. Signatures come from the pinned model lock (file hashes), not display names.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ...models import load_lock
from ...taskstore import NewTask
from ..runner import TaskEnv


@dataclass(frozen=True)
class Stage:
    name: str
    family: str  # item-level grouping shown in the UI: enhance | generate | qa | build | publish
    lane: str
    worker: str | None  # GPU1 worker leased for the pass ("aux" | "worker3d"), None otherwise
    run: Callable[[TaskEnv], dict[str, Any]]
    coalesce: bool = False  # wait for a cross-Job window before starting a pass (mask/VLM QA)
    on_error: Callable[[TaskEnv, str, dict[str, Any]], None] | None = None


@lru_cache(maxsize=64)
def _identity(config_dir: str, key: str) -> str:
    lock = load_lock(Path(config_dir))
    entry = (lock.get("models") or {}).get(key) or (lock.get("loras") or {}).get(key) or {}
    blob = json.dumps({"repo": entry.get("repo"), "revision": entry.get("revision"),
                       "files": entry.get("files")}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def model_identity(env_or_config: Any, key: str) -> str:
    config_dir = env_or_config if isinstance(env_or_config, Path) else env_or_config.settings.config_dir
    return f"{key}@{_identity(str(config_dir), key)}"


def residency(studio: Any, stage: str, **kw: Any) -> str:
    """Static residencies per stage; image generation adds its weight-changing modifiers."""
    if stage in ("enhance", "qa_vlm", "style_analyze", "style_synthesize", "style_evaluate"):
        return f"aux.vlm:{model_identity(studio, 'qwen3_vl_8b_instruct')}"
    if stage in ("mask", "segment"):
        return f"aux.birefnet:{model_identity(studio, 'birefnet')}"
    if stage == "sample":
        return (f"worker3d.trellis:{model_identity(studio, 'trellis2')}+{model_identity(studio, 'dinov3_vitl16')}"
                f"+{model_identity(studio, 'trellis_image_large')}")
    if stage == "bake":
        return f"worker3d.bake:{kw.get('exporter', 'clean')}"
    if stage == "generate":
        speed = kw.get("speed_preset") or "quality"
        lora = kw.get("style_lora")
        return (f"comfyui:{model_identity(studio, 'qwen_image_2512')}|speed={speed}"
                f"{'@' + model_identity(studio, speed) if speed != 'quality' else ''}"
                f"|style_lora={lora or 'none'}")
    return "cpu"


def new_task(studio: Any, stage: Stage, *, project_id: str, job_id: str, item_id: str, input_key: str,
             inputs: dict[str, Any], deps: list[str] | None = None, run_id: str | None = None,
             wave_id: str | None = None, priority: int = 100, lane: str | None = None,
             resident: str | None = None, microbatch: str = "") -> NewTask:
    return NewTask(project_id=project_id, job_id=job_id, item_id=item_id, stage=stage.name, family=stage.family,
                   input_key=input_key, inputs=inputs, lane=lane or stage.lane,
                   residency=resident or residency(studio, stage.name, **inputs), priority=priority,
                   deps=deps or [], run_id=run_id, wave_id=wave_id, microbatch=microbatch)
