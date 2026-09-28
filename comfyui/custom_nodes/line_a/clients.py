"""HTTP clients for prompt-service and trellis-worker (both on GPU1, time-shared)."""
from __future__ import annotations

from typing import Any

import requests

from .settings import app_config


class ServiceError(RuntimeError):
    pass


def _post(base_key: str, path: str, payload: dict[str, Any] | None, timeout_s: float) -> dict[str, Any]:
    url = app_config()["services"][base_key].rstrip("/") + path
    try:
        r = requests.post(url, json=payload or {}, timeout=timeout_s)
    except requests.RequestException as e:
        raise ServiceError(f"{url}: {e}") from e
    if r.status_code >= 400:
        raise ServiceError(f"{url} -> {r.status_code}: {r.text[:1000]}")
    return r.json()


def _timeout(key: str) -> float:
    return float(app_config()["timeouts"][key])


def _unload(base_key: str) -> None:
    try:
        _post(base_key, "/unload", None, 60)
    except ServiceError:
        pass  # service down => it holds no VRAM; the real call that follows reports real errors


def free_gpu1_for_prompt_service() -> None:
    _unload("trellis_worker_url")


def free_gpu1_for_trellis() -> None:
    _unload("prompt_service_url")


def enhance(prompt: str, asset_type: str | None, target_triangles: int | None) -> dict[str, Any]:
    payload = {"prompt": prompt, "asset_type": asset_type, "target_triangles": target_triangles}
    return _post("prompt_service_url", "/enhance", payload, _timeout("enhance_s"))


def vlm_qa(image_path: str, prompt: str | None) -> dict[str, Any]:
    return _post("prompt_service_url", "/qa", {"image_path": image_path, "prompt": prompt}, _timeout("qa_per_image_s"))


def cutout(job_id: str, src: str, dst_rgba: str, dst_mask: str | None, **kw: Any) -> dict[str, Any]:
    payload = {"job_id": job_id, "src": src, "dst_rgba": dst_rgba, "dst_mask": dst_mask, **kw}
    return _post("trellis_worker_url", "/cutout", payload, _timeout("cutout_s"))


def trellis_generate(job_id: str, **kw: Any) -> dict[str, Any]:
    return _post("trellis_worker_url", "/generate", {"job_id": job_id, **kw}, _timeout("trellis_s"))
