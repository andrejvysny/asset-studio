"""Client for the GPU1 3D worker (TRELLIS.2 sampling + GLB export). Bytes over HTTP; the Studio stores results."""
from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from .base import AckError, EngineRejected, EngineUnavailable

TIMEOUT = httpx.Timeout(1800.0, connect=5.0)  # 1536_cascade sampling can take minutes


class Worker3dClient:
    name = "worker3d"
    simulated = False

    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = client or httpx.Client(base_url=self.base_url, timeout=TIMEOUT)

    def _bytes(self, r: httpx.Response, what: str) -> tuple[bytes, dict[str, Any]]:
        if r.status_code in (400, 413, 422):
            raise EngineRejected(f"worker3d {what}: {r.text[:300]}")
        if r.status_code != 200:
            raise EngineUnavailable(f"worker3d {what}: HTTP {r.status_code} {r.text[:200]}")
        try:
            meta = json.loads(r.headers.get("x-worker-meta", "{}"))
        except ValueError as e:
            raise EngineUnavailable(f"worker3d {what}: malformed metadata") from e
        if not r.content or not isinstance(meta, dict):
            raise EngineUnavailable(f"worker3d {what}: empty response")
        return r.content, meta

    def health(self) -> dict[str, Any]:
        try:
            r = self.http.get("/health", timeout=5.0)
            body = r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return {"reachable": False}
        return {"reachable": r.status_code == 200, **body}

    def generate(self, *, image_rgba: bytes, seed: int, pipeline_type: str) -> tuple[bytes, dict[str, Any]]:
        try:
            r = self.http.post("/generate", json={"image_b64": base64.b64encode(image_rgba).decode(), "seed": seed,
                                                  "pipeline_type": pipeline_type})
        except httpx.HTTPError as e:
            raise EngineUnavailable(f"worker3d /generate: {type(e).__name__}") from e
        return self._bytes(r, "/generate")

    def export(self, *, raw: bytes, exporter: str, decimation_target: int, texture_size: int,
               remesh: bool) -> tuple[bytes, dict[str, Any]]:
        params = {"exporter": exporter, "decimation_target": decimation_target, "texture_size": texture_size,
                  "remesh": str(remesh).lower()}
        try:
            r = self.http.post("/export", params=params, content=raw,
                               headers={"content-type": "application/octet-stream"})
        except httpx.HTTPError as e:
            raise EngineUnavailable(f"worker3d /export: {type(e).__name__}") from e
        return self._bytes(r, "/export")

    def unload(self, owner_token: str) -> dict[str, Any]:
        """Only an explicit 200 body {loaded: false, owner_token: <ours>} counts as released."""
        try:
            r = self.http.post("/unload", json={"owner_token": owner_token}, timeout=300.0)
        except httpx.HTTPError as e:
            raise AckError(f"unload not acknowledged: {type(e).__name__}") from e
        if r.status_code != 200:
            raise AckError(f"unload not acknowledged: HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError as e:
            raise AckError("unload not acknowledged: malformed body") from e
        if not isinstance(body, dict) or body.get("loaded") is not False or body.get("owner_token") != owner_token:
            raise AckError(f"unload not acknowledged: {str(body)[:200]}")
        return body
