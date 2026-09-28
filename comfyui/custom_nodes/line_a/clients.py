"""HTTP clients for prompt-service and trellis-worker (both on GPU1, time-shared)."""
from __future__ import annotations

import json
from typing import Any

import requests

from .settings import app_config


class ServiceError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, stage: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.stage = stage  # worker-reported pipeline stage that failed, if any


class ServiceDown(ServiceError):
    """Connection refused: the service process is not running."""


def _post(base_key: str, path: str, payload: dict[str, Any] | None, timeout_s: float) -> dict[str, Any]:
    url = app_config()["services"][base_key].rstrip("/") + path
    try:
        r = requests.post(url, json=payload or {}, timeout=(5, timeout_s))
    except requests.ConnectionError as e:
        raise ServiceDown(f"{url}: {e}") from e
    except requests.RequestException as e:
        raise ServiceError(f"{url}: {e}") from e
    if r.status_code >= 400:
        stage, detail = None, r.text[:1000]
        try:
            body = json.loads(r.text).get("detail")
            if isinstance(body, dict):
                stage, detail = body.get("stage"), body.get("error", detail)
            elif body:
                detail = str(body)
        except (ValueError, AttributeError):
            pass
        raise ServiceError(f"{path} -> {r.status_code}: {detail}", status=r.status_code, stage=stage)
    return r.json()


def _timeout(key: str) -> float:
    return float(app_config()["timeouts"][key])


def _unload(base_key: str) -> None:
    """Fail closed: only 'not running' is treated as released. Timeouts/errors abort the hand-off."""
    try:
        _post(base_key, "/unload", None, 60)
    except ServiceDown:
        pass
    except ServiceError as e:
        raise ServiceError(f"GPU1 hand-off failed, {base_key} did not confirm unload: {e}") from e


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


def trellis_reexport(job_id: str, **kw: Any) -> dict[str, Any]:
    return _post("trellis_worker_url", "/reexport", {"job_id": job_id, **kw}, _timeout("trellis_s"))
