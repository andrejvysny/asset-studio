"""Direct-mode GPU facts: utilisation unknown is reported as null, never as idle (0)."""
from __future__ import annotations

from types import SimpleNamespace

from assetstudio_server.services.runtime import worker_gpus

_STUDIO = SimpleNamespace(settings=SimpleNamespace(gpu_ids={"gpu0": 0, "gpu1": 1}))
_ENG = {"devices": [{"name": "cuda:0 : NVIDIA GPU", "vram_total": 2**30, "vram_free": 2**29}]}


def test_comfyui_gpu_reports_unknown_utilisation() -> None:
    (g,) = worker_gpus(_STUDIO, _ENG, {})  # type: ignore[arg-type]
    assert g["util_pct"] is None and g["vram_used_mb"] == 512


def test_aux_gpu_without_utilisation_is_null_and_reported_value_is_kept() -> None:
    gpu = {"name": "A", "vram_used_mb": 1, "vram_total_mb": 2}
    assert worker_gpus(_STUDIO, {}, {"gpu": gpu})[0]["util_pct"] is None  # type: ignore[arg-type]
    assert worker_gpus(_STUDIO, {}, {"gpu": {**gpu, "util_pct": 37}})[0]["util_pct"] == 37  # type: ignore[arg-type]
