"""Client for the GPU1 aux service (text enhancement, VLM QA, BiRefNet segmentation). Bytes over HTTP only."""
from __future__ import annotations

import base64
from typing import Any

import httpx

from .base import EngineRejected, EngineUnavailable, post_ack

TIMEOUT = httpx.Timeout(300.0, connect=5.0)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class AuxClient:
    name = "aux"
    simulated = False

    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = client or httpx.Client(base_url=self.base_url, timeout=TIMEOUT)

    def _post(self, path: str, payload: dict[str, Any], epoch: int, execution_id: str | None) -> dict[str, Any]:
        headers = {"x-lease-epoch": str(epoch), **({"x-execution-id": execution_id} if execution_id else {})}
        try:
            r = self.http.post(path, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise EngineUnavailable(f"aux {path}: {type(e).__name__}") from e
        if r.status_code in (400, 422):
            raise EngineRejected(f"aux {path}: {r.text[:300]}")
        if r.status_code == 409:  # stale lease: this Studio no longer owns the device
            raise EngineUnavailable(f"aux {path}: {r.text[:200]}")
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

    def enhance(self, *, brief: str, kind: str, constraints: str, style_guide: str, epoch: int,
                execution_id: str | None = None, preset: str = "conservative", mode: str = "t2i",
                images: list[tuple[bytes, str, str]] | None = None, preserve: str = "",
                change: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {"brief": brief, "kind": kind, "constraints": constraints,
                                   "style_guide": style_guide}
        if preset != "conservative" or mode != "t2i" or images or preserve or change:
            payload |= {"preset": preset, "mode": mode, "preserve": preserve, "change": change,
                        "images": [{"b64": _b64(b), "role": role, "note": note} for b, role, note in images or []]}
        return self._post("/enhance", payload, epoch, execution_id)

    def compare(self, *, images: list[tuple[bytes, str, str]], questions: list[tuple[str, str]], context: str,
                epoch: int, execution_id: str | None = None) -> dict[str, Any]:
        return self._post("/compare", {
            "images": [{"b64": _b64(b), "label": label, "note": note} for b, label, note in images],
            "questions": [{"id": i, "question": q} for i, q in questions], "context": context}, epoch, execution_id)

    def analyze_source(self, *, images: list[tuple[bytes, str]], kind: str, user_facts: str = "", epoch: int,
                       execution_id: str | None = None) -> dict[str, Any]:
        return self._post("/analyze_source", {"images": [{"b64": _b64(b), "view": v} for b, v in images],
                                              "kind": kind, "user_facts": user_facts}, epoch, execution_id)

    def suggest_variants(self, *, images: list[tuple[bytes, str]], request: str, count: int, intent: str,
                         preserve: str, kind: str, observations: list[str] | None = None, epoch: int,
                         execution_id: str | None = None) -> dict[str, Any]:
        return self._post("/suggest_variants", {
            "images": [{"b64": _b64(b), "view": v} for b, v in images], "request": request, "count": count,
            "intent": intent, "preserve": preserve, "kind": kind, "observations": observations or []},
            epoch, execution_id)

    def qa(self, *, image: bytes, questions: list[tuple[str, str]], context: str, epoch: int,
           execution_id: str | None = None) -> dict[str, Any]:
        return self._post("/qa", {"image_b64": base64.b64encode(image).decode(), "context": context,
                                  "questions": [{"id": i, "question": q} for i, q in questions]}, epoch, execution_id)

    def cutout(self, *, image: bytes, epoch: int, execution_id: str | None = None) -> dict[str, Any]:
        body = self._post("/cutout", {"image_b64": base64.b64encode(image).decode()}, epoch, execution_id)
        try:
            body["mask_png"] = base64.b64decode(body.pop("mask_b64"), validate=True)
        except (KeyError, ValueError) as e:
            raise EngineUnavailable("aux /cutout: malformed mask") from e
        return body

    def lease(self, epoch: int) -> dict[str, Any]:
        return post_ack(self.http, "/lease", {"epoch": epoch}, 30.0)

    def unload(self, owner_token: str, epoch: int) -> dict[str, Any]:
        """Validated by the GPU lane: {loaded: false, active: 0, admitting: false, epoch, owner_token}."""
        return post_ack(self.http, "/unload", {"owner_token": owner_token, "epoch": epoch}, 330.0)
