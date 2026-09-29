"""Client for the GPU1 aux service (text enhancement, VLM QA, BiRefNet segmentation). Bytes over HTTP only."""
from __future__ import annotations

import base64
from typing import Any

import httpx

from .base import AckError, EngineRejected, EngineUnavailable

TIMEOUT = httpx.Timeout(300.0, connect=5.0)


class AuxClient:
    name = "aux"
    simulated = False

    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = client or httpx.Client(base_url=self.base_url, timeout=TIMEOUT)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            r = self.http.post(path, json=payload)
        except httpx.HTTPError as e:
            raise EngineUnavailable(f"aux {path}: {type(e).__name__}") from e
        if r.status_code in (400, 422):
            raise EngineRejected(f"aux {path}: {r.text[:300]}")
        if r.status_code != 200:
            raise EngineUnavailable(f"aux {path}: HTTP {r.status_code} {r.text[:200]}")
        try:
            body = r.json()
        except ValueError as e:
            raise EngineUnavailable(f"aux {path}: malformed response") from e
        if not isinstance(body, dict):
            raise EngineUnavailable(f"aux {path}: malformed response")
        return body

    def health(self) -> dict[str, Any]:
        try:
            r = self.http.get("/health", timeout=5.0)
            body = r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return {"reachable": False}
        return {"reachable": r.status_code == 200, **body}

    def enhance(self, *, brief: str, kind: str, constraints: str, style_guide: str) -> dict[str, Any]:
        return self._post("/enhance", {"brief": brief, "kind": kind, "constraints": constraints,
                                       "style_guide": style_guide})

    def qa(self, *, image: bytes, questions: list[tuple[str, str]], context: str) -> dict[str, Any]:
        return self._post("/qa", {"image_b64": base64.b64encode(image).decode(), "context": context,
                                  "questions": [{"id": i, "question": q} for i, q in questions]})

    def cutout(self, *, image: bytes) -> dict[str, Any]:
        body = self._post("/cutout", {"image_b64": base64.b64encode(image).decode()})
        try:
            body["mask_png"] = base64.b64decode(body.pop("mask_b64"), validate=True)
        except (KeyError, ValueError) as e:
            raise EngineUnavailable("aux /cutout: malformed mask") from e
        return body

    def unload(self, owner_token: str) -> dict[str, Any]:
        """Only an explicit 200 body {loaded: false, owner_token: <ours>} counts as released."""
        try:
            r = self.http.post("/unload", json={"owner_token": owner_token}, timeout=60.0)
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
