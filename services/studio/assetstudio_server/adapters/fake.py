"""SIMULATED engines for tests and the opt-in demo. Every output is labelled simulated; never production evidence."""
from __future__ import annotations

import hashlib
import io
import threading
from typing import Any

from PIL import Image, ImageDraw

from .base import EngineUnavailable, JobStatus, T2IRequest


def _png(seed: int, w: int, h: int, label: str) -> bytes:
    rnd = hashlib.sha256(str(seed).encode()).digest()
    base = tuple(40 + b % 150 for b in rnd[:3])
    im = Image.new("RGB", (w, h), (208, 208, 204))
    d = ImageDraw.Draw(im)
    r = int(min(w, h) * (0.22 + rnd[3] / 255 * 0.12))
    cx, cy = w // 2 + (rnd[4] - 128) * w // 2000, h // 2 + (rnd[5] - 128) * h // 2000
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=base)
    d.text((8, 8), f"SIMULATED {label}", fill=(60, 60, 60))
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


class FakeEngine:
    name = "fake"
    simulated = True

    def __init__(self, steps_to_finish: int = 1) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.steps_to_finish = steps_to_finish
        self.fail_prompts: set[str] = set()
        self.lose_ack = 0  # simulate "request reached the engine but the response was lost"
        self.down = False
        self._lock = threading.Lock()

    def check(self) -> dict[str, Any]:
        return {"reachable": not self.down, "ready": not self.down, "problems": [], "simulated": True}

    def submit(self, req: T2IRequest) -> str:
        with self._lock:
            self.calls.append(("submit", req.prompt_id))
            if self.down:
                raise EngineUnavailable("simulated outage")
            self.jobs.setdefault(req.prompt_id, {"req": req, "polls": 0})
            if self.lose_ack > 0:
                self.lose_ack -= 1
                raise EngineUnavailable("simulated lost submit response")
        return req.prompt_id

    def status(self, prompt_id: str) -> JobStatus:
        with self._lock:
            if self.down:
                raise EngineUnavailable("simulated outage")
            job = self.jobs.get(prompt_id)
            if job is None:
                return JobStatus("unknown")
            job["polls"] += 1
            if prompt_id in self.fail_prompts:
                return JobStatus("failed", "simulated failure")
            return JobStatus("succeeded" if job["polls"] >= self.steps_to_finish else "running")

    def fetch_image(self, prompt_id: str) -> bytes:
        req: T2IRequest = self.jobs[prompt_id]["req"]
        return _png(req.seed, min(req.width, 512), min(req.height, 512), f"seed {req.seed}")

    def cancel(self, prompt_id: str) -> dict[str, Any]:
        with self._lock:
            self.calls.append(("cancel", prompt_id))
            self.jobs.pop(prompt_id, None)
        return {"requested": True}

    def describe(self) -> dict[str, Any]:
        return {"engine": "fake", "simulated": True, "workflow": "fake.t2i", "workflow_version": 0}


class FakeAux:
    name = "aux"
    simulated = True

    def __init__(self) -> None:
        self.loaded = False
        self.calls: list[str] = []
        self.unload_response: dict[str, Any] | Exception | None = None
        self.vlm_answers: dict[str, Any] | None = None

    def health(self) -> dict[str, Any]:
        return {"reachable": True, "simulated": True, "loaded": {"vlm": self.loaded, "birefnet": self.loaded}}

    def enhance(self, *, brief: str, kind: str, constraints: str, style_guide: str) -> dict[str, Any]:
        self.calls.append("enhance")
        self.loaded = True
        text = brief.strip().rstrip(".")
        return {"description": f"{text[:1].upper()}{text[1:]}, clearly readable form, simulated enhancement.",
                "short_title": text[:30], "tags": [kind], "meta": {"model": "simulated", "seconds": 0.0,
                                                                   "raw": "simulated"}}

    def qa(self, *, image: bytes, questions: list[tuple[str, str]], context: str) -> dict[str, Any]:
        self.calls.append("qa")
        self.loaded = True
        if self.vlm_answers is not None:
            return {"checks": self.vlm_answers, "reasons": [], "summary": "simulated", "meta": {"model": "simulated"}}
        h = hashlib.sha256(image).digest()
        return {"checks": {qid: h[i % len(h)] > 30 for i, (qid, _) in enumerate(questions)},
                "reasons": [], "summary": "simulated answers", "meta": {"model": "simulated"}}

    def cutout(self, *, image: bytes) -> dict[str, Any]:
        self.calls.append("cutout")
        self.loaded = True
        with Image.open(io.BytesIO(image)) as im:
            w, h = im.size
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).ellipse((w * 0.25, h * 0.25, w * 0.75, h * 0.75), fill=255)
        out = io.BytesIO()
        mask.save(out, "PNG")
        return {"mask_png": out.getvalue(), "meta": {"model": "simulated"}}

    def unload(self, owner_token: str) -> dict[str, Any]:
        self.calls.append("unload")
        if isinstance(self.unload_response, Exception):
            raise self.unload_response
        self.loaded = False
        return self.unload_response or {"loaded": False, "owner_token": owner_token}
