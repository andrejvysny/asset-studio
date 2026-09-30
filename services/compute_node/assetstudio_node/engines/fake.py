"""SIMULATED engines for tests and the opt-in demo. Every output is labelled simulated; never production evidence."""
from __future__ import annotations

import hashlib
import io
import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from assetstudio_core.canonical import sha256_json
from PIL import Image, ImageDraw

from .base import (
    AckError,
    EngineRejected,
    EngineUnavailable,
    ExecutionFailed,
    ExecutionLost,
    ImageEditRequest,
    JobStatus,
    T2IRequest,
)


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


def _edit_png(source: bytes, seed: int) -> bytes:
    """Deterministic 'edit': hue-shifted source plus a text-free overlay; output depends on the source bytes."""
    rnd = hashlib.sha256(f"edit:{seed}".encode()).digest()
    with Image.open(io.BytesIO(source)) as src:
        im = src.convert("RGB")
    h, s_, v = im.convert("HSV").split()
    shift = 16 + rnd[0] % 224
    h = h.point(lambda x: (x + shift) % 256)
    im = Image.merge("HSV", (h, s_, v)).convert("RGB")
    w, ht = im.size
    d = ImageDraw.Draw(im)
    x0, y0 = w * (5 + rnd[1] % 20) // 100, ht * (5 + rnd[2] % 20) // 100
    d.rectangle((x0, y0, x0 + max(4, w // 5), y0 + max(4, ht // 8)), outline=(255, 255, 255), width=max(1, w // 128))
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
        self.queue_ids: set[str] = set()  # test hook: prompt ids the "engine" reports as queued/running
        self._lock = threading.Lock()

    def queue_prompt_ids(self) -> set[str]:
        if self.down:
            raise EngineUnavailable("simulated outage")
        return set(self.queue_ids)

    def check(self) -> dict[str, Any]:
        return {"reachable": not self.down, "ready": not self.down, "problems": [], "simulated": True,
                "workflows": {"fake.t2i": {"kind": "t2i", "ready": True, "problems": []},
                              "fake.image_edit": {"kind": "image_edit", "ready": True, "problems": []}}}

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

    def supports(self, kind: str) -> bool:
        return kind in ("t2i", "image_edit")

    def submit_edit(self, req: ImageEditRequest) -> dict[str, Any]:
        if hashlib.sha256(req.image).hexdigest() != req.prepared_input_sha256:
            raise EngineRejected("prepared input hash mismatch")
        with self._lock:
            self.calls.append(("submit_edit", req.prompt_id))
            if self.down:
                raise EngineUnavailable("simulated outage")
            self.jobs.setdefault(req.prompt_id, {"req": req, "polls": 0})
            if self.lose_ack > 0:
                self.lose_ack -= 1
                raise EngineUnavailable("simulated lost submit response")
        digest = req.prepared_input_sha256
        graph = {"simulated": True, "seed": req.seed, "steps": req.steps, "cfg": req.cfg, "input": digest}
        return {"prompt_id": req.prompt_id, "workflow": "fake.image_edit", "workflow_version": 0,
                "graph_sha256": sha256_json(graph),
                "input": {"name": f"as_{digest[:32]}.png", "subfolder": "assetstudio", "sha256": digest}}

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

    def fetch_image(self, prompt_id: str, workflow_id: str | None = None) -> bytes:
        req = self.jobs[prompt_id]["req"]
        if isinstance(req, ImageEditRequest):
            return _edit_png(req.image, req.seed)
        return _png(req.seed, min(req.width, 512), min(req.height, 512), f"seed {req.seed}")

    def cancel(self, prompt_id: str) -> dict[str, Any]:
        with self._lock:
            self.calls.append(("cancel", prompt_id))
            self.jobs.pop(prompt_id, None)
        return {"requested": True}

    def describe(self) -> dict[str, Any]:
        return {"engine": "fake", "simulated": True, "workflow": "fake.t2i", "workflow_version": 0}


class _FakeLease:
    """In-process mirror of services/worker_common/lease.py semantics (epoch fencing + drain on unload).

    `activity()` counts in-flight GPU work; `drain` stops admitting and waits up to `drain_timeout` seconds for
    it to finish before acknowledging (0 = refuse immediately while work is active)."""

    def __init__(self) -> None:
        self.session_id = f"fake-{id(self):x}"
        self.epoch, self.admitting, self.active = 0, False, 0
        self.unload_response: dict[str, Any] | Exception | None = None
        self.lease_error: Exception | None = None
        self.drain_timeout = 0.0
        self._cond = threading.Condition(threading.RLock())

    def lease(self, epoch: int) -> dict[str, Any]:
        if self.lease_error is not None:
            raise self.lease_error
        if epoch < self.epoch:
            raise AckError(f"stale lease {epoch} < {self.epoch}")
        self.epoch, self.admitting = epoch, True
        return self.info()

    def info(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "epoch": self.epoch, "admitting": self.admitting,
                "active": self.active}

    def check(self, epoch: int) -> None:
        if epoch != self.epoch or not self.admitting:
            raise EngineUnavailable(f"stale_lease: epoch {epoch} is not admitted ({self.epoch})")

    @contextmanager
    def activity(self, epoch: int) -> Iterator[None]:
        with self._cond:
            self.check(epoch)
            self.active += 1
        try:
            yield
        finally:
            with self._cond:
                self.active -= 1
                self._cond.notify_all()

    def drain(self, owner_token: str, epoch: int) -> dict[str, Any]:
        if isinstance(self.unload_response, Exception):
            raise self.unload_response
        if self.unload_response is not None:
            return self.unload_response
        with self._cond:
            self.epoch, self.admitting = max(self.epoch, epoch), False
            if not self._cond.wait_for(lambda: self.active == 0, timeout=self.drain_timeout):
                raise AckError("GPU work still active")
            return {**self.info(), "loaded": False, "owner_token": owner_token}


_FAKE_VARIANTS = [
    ("Taller", "Taller body; keep materials and identity"),
    ("Squat", "Shorter and wider; same materials"),
    ("Slimmer", "Narrower silhouette"),
    ("Weathered", "Worn, faded surfaces"),
    ("Damaged", "One broken part, otherwise intact"),
    ("Detailed", "One extra decorative detail"),
]


class FakeAux:
    name = "aux"
    simulated = True

    def __init__(self) -> None:
        self.loaded: dict[str, bool] = {"vlm": False, "birefnet": False}
        self.loads: dict[str, int] = {"vlm": 0, "birefnet": 0}
        self.calls: list[str] = []
        self.vlm_answers: dict[str, Any] | None = None
        self.during_compare: Callable[[int], None] | None = None  # test hook: called with the compare ordinal
        self.gpu = _FakeLease()

    @property
    def unload_response(self) -> dict[str, Any] | Exception | None:
        return self.gpu.unload_response

    @unload_response.setter
    def unload_response(self, value: dict[str, Any] | Exception | None) -> None:
        self.gpu.unload_response = value

    def _use(self, component: str, epoch: int) -> None:
        self.gpu.check(epoch)
        if not self.loaded[component]:
            self.loaded[component] = True
            self.loads[component] += 1

    def health(self) -> dict[str, Any]:
        return {"reachable": True, "simulated": True, "loaded": dict(self.loaded), "loads": dict(self.loads),
                "lease": self.gpu.info()}

    def lease(self, epoch: int) -> dict[str, Any]:
        return self.gpu.lease(epoch)

    def enhance(self, *, brief: str, kind: str, constraints: str, style_guide: str, epoch: int,
                execution_id: str | None = None, preset: str = "conservative", mode: str = "t2i",
                images: list[tuple[bytes, str, str]] | None = None, preserve: str = "",
                change: str = "") -> dict[str, Any]:
        self._use("vlm", epoch)
        self.calls.append("enhance")
        text = brief.strip().rstrip(".")
        if mode == "edit":
            desc = (f"Edit the source object: {change or text}. Keep: {preserve or 'its identity'}. "
                    "Use the input image as the identity reference; single object, no collage, no scene, "
                    "no duplicates. Simulated instruction.")
        else:
            desc = f"{text[:1].upper()}{text[1:]}, clearly readable form, simulated enhancement."
        refs = [note for _, role, note in images or [] if role == "reference"]
        return {"description": desc, "short_title": text[:30], "tags": [kind], "facts": [text] if text else [],
                "additions": ["simulated addition: finer surface detail"] if preset == "creative" else [],
                "assumptions": [], "reference_cues": [{"index": i, "cue": n or "(no note)"}
                                                      for i, n in enumerate(refs)],
                "meta": {"model": "simulated", "simulated": True, "seconds": 0.0, "raw": "simulated",
                         "execution_id": execution_id}}

    def compare(self, *, images: list[tuple[bytes, str, str]], questions: list[tuple[str, str]], context: str,
                epoch: int, execution_id: str | None = None) -> dict[str, Any]:
        self._use("vlm", epoch)
        self.calls.append("compare")
        with self.gpu.activity(epoch):
            if self.during_compare is not None:
                self.during_compare(len([c for c in self.calls if c == "compare"]))
            return self._compare(images, questions, execution_id)

    def _compare(self, images: list[tuple[bytes, str, str]], questions: list[tuple[str, str]],
                 execution_id: str | None) -> dict[str, Any]:
        src = next((b for b, label, _ in images if label == "source"), None)
        cand = next((b for b, label, _ in images if label == "candidate"), None)
        checks: dict[str, bool | str] = {}
        reasons: dict[str, str] = {}
        for qid, _ in questions:
            if "change" in qid:
                checks[qid] = not (src is not None and cand is not None and src == cand)
                reasons[qid] = "simulated: candidate differs from source" if checks[qid] \
                    else "simulated: candidate is identical to source"
            else:
                checks[qid], reasons[qid] = True, "simulated: assumed satisfied"
        return {"checks": checks, "reasons": reasons,
                "meta": {"model": "simulated", "simulated": True, "seconds": 0.0, "execution_id": execution_id}}

    def analyze_source(self, *, images: list[tuple[bytes, str]], kind: str, user_facts: str = "", epoch: int,
                       execution_id: str | None = None) -> dict[str, Any]:
        self._use("vlm", epoch)
        self.calls.append("analyze_source")
        return {"observations": [{"text": "simulated observation: single object", "images": [0]}],
                "uncertainties": ["simulated: materials inferred"],
                "proposed_preserve": [{"id": "preserve_identity", "text": "Keep the same asset identity."}],
                "proposed_changeable": ["simulated: proportions"], "dropped_observations": 0,
                "meta": {"model": "simulated", "simulated": True, "seconds": 0.0, "execution_id": execution_id}}

    def suggest_variants(self, *, images: list[tuple[bytes, str]], request: str, count: int, intent: str,
                         preserve: str, kind: str, observations: list[str] | None = None, epoch: int,
                         execution_id: str | None = None) -> dict[str, Any]:
        self._use("vlm", epoch)
        self.calls.append("suggest_variants")
        rows = []
        for i in range(count):
            label, change = _FAKE_VARIANTS[i % len(_FAKE_VARIANTS)]
            n = i // len(_FAKE_VARIANTS)
            rows.append({"label": label if n == 0 else f"{label} {n + 1}", "change_request": change})
        return {"rows": rows, "short_by": 0, "notes": ["simulated suggestions"],
                "meta": {"model": "simulated", "simulated": True, "seconds": 0.0, "execution_id": execution_id}}

    def qa(self, *, image: bytes, questions: list[tuple[str, str]], context: str, epoch: int,
           execution_id: str | None = None) -> dict[str, Any]:
        self._use("vlm", epoch)
        self.calls.append("qa")
        if self.vlm_answers is not None:
            return {"checks": self.vlm_answers, "reasons": [], "summary": "simulated", "meta": {"model": "simulated"}}
        h = hashlib.sha256(image).digest()
        return {"checks": {qid: h[i % len(h)] > 30 for i, (qid, _) in enumerate(questions)},
                "reasons": [], "summary": "simulated answers", "meta": {"model": "simulated"}}

    def cutout(self, *, image: bytes, epoch: int, execution_id: str | None = None) -> dict[str, Any]:
        self._use("birefnet", epoch)
        self.calls.append("cutout")
        with Image.open(io.BytesIO(image)) as im:
            w, h = im.size
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).ellipse((w * 0.25, h * 0.25, w * 0.75, h * 0.75), fill=255)
        out = io.BytesIO()
        mask.save(out, "PNG")
        return {"mask_png": out.getvalue(), "meta": {"model": "simulated"}}

    def unload(self, owner_token: str, epoch: int) -> dict[str, Any]:
        self.calls.append("unload")
        body = self.gpu.drain(owner_token, epoch)
        if body.get("loaded") is False:
            self.loaded = {k: False for k in self.loaded}
        return body


class FakeWorker3d:
    """SIMULATED 3D worker: a textured sphere, never a reconstruction. Same execution protocol as the real one:
    ids chosen by the caller, idempotent submit, status/result/ack, `lost` after a simulated restart."""

    name = "worker3d"
    simulated = True

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.loaded = False
        self.loads = 0
        self.gpu = _FakeLease()
        self.executions: dict[str, dict[str, Any]] = {}
        self.fail_ops: dict[str, str] = {}  # op -> failure code for the next execution of that op
        self.lose_submit_response = 0  # admitted, but the HTTP response is "lost"
        self.hold = False  # keep new executions "running" until release_held()

    @property
    def unload_response(self) -> dict[str, Any] | Exception | None:
        return self.gpu.unload_response

    @unload_response.setter
    def unload_response(self, value: dict[str, Any] | Exception | None) -> None:
        self.gpu.unload_response = value

    def health(self) -> dict[str, Any]:
        return {"reachable": True, "ok": True, "missing_models": [], "exporters": {"clean": True, "research": False},
                "loaded": {"trellis2": self.loaded}, "loads": {"trellis2": self.loads}, "simulated": True,
                "lease": self.gpu.info(), "spooled": len(self.executions)}

    def lease(self, epoch: int) -> dict[str, Any]:
        return self.gpu.lease(epoch)

    def unload(self, owner_token: str, epoch: int) -> dict[str, Any]:
        self.calls.append("unload")
        body = self.gpu.drain(owner_token, epoch)
        if body.get("loaded") is False:
            self.loaded = False
        return body

    def restart(self) -> None:
        """Simulated worker process restart: unfinished executions become `lost`; spooled results survive."""
        for e in self.executions.values():
            if e["state"] in ("queued", "running"):
                e["state"], e["error"] = "lost", "worker restarted before this execution finished"
        self.gpu = _FakeLease()
        self.gpu.session_id = f"fake-restarted-{len(self.calls)}"
        self.loaded = False

    def release_held(self) -> None:
        for eid, e in self.executions.items():
            if e["state"] == "running":
                self._finish(eid)

    def status(self, execution_id: str) -> dict[str, Any] | None:
        e = self.executions.get(execution_id)
        return None if e is None else {k: v for k, v in e.items() if k not in ("result", "body")}

    def ack(self, execution_id: str) -> None:
        e = self.executions.get(execution_id)
        if e is not None and e["state"] in ("succeeded", "failed", "cancelled", "lost"):
            del self.executions[execution_id]

    def execute(self, execution_id: str, op: str, params: dict[str, Any], body: bytes, *, epoch: int,
                should_cancel: Any = None) -> tuple[bytes, dict[str, Any]]:
        req = hashlib.sha256(json.dumps({"op": op, "params": params}, sort_keys=True).encode() + body).hexdigest()
        e = self.executions.get(execution_id)
        if e is None:
            self.gpu.check(epoch)
            if op == "export" and not body.startswith(b"SIMULATED-RAW:"):
                raise ExecutionFailed("invalid raw intermediate", "input_invalid")
            self.calls.append(op if op == "generate" else f"export:{params['exporter']}")
            self.executions[execution_id] = e = {"state": "running", "op": op, "params": params, "body": body,
                                                 "request_sha256": req, "session_id": self.gpu.session_id}
            if not self.hold:
                self._finish(execution_id)
            if self.lose_submit_response > 0:
                self.lose_submit_response -= 1
                raise EngineUnavailable("simulated lost submit response")
        elif e["request_sha256"] != req:
            raise EngineUnavailable("execution id reused with a different request")
        if e["state"] == "running":
            raise EngineUnavailable("simulated: still running")
        if e["state"] == "failed":
            raise ExecutionFailed(e["error"], e["code"])
        if e["state"] == "lost":
            raise ExecutionLost(e["error"])
        return e["result"], {**e["meta"], "execution_id": execution_id, "worker_session": e["session_id"]}

    def _finish(self, eid: str) -> None:
        e = self.executions[eid]
        if (code := self.fail_ops.pop(e["op"], None)) is not None:
            e.update(state="failed", error=f"simulated {code}", code=code)
            return
        if e["op"] == "generate":
            if not self.loaded:
                self.loaded, self.loads = True, self.loads + 1
            p = e["params"]
            raw = b"SIMULATED-RAW:" + hashlib.sha256(e["body"] + int(p["seed"]).to_bytes(8, "big")).digest()
            meta = {"engine": "simulated", "seed": p["seed"], "pipeline_type": p["pipeline_type"], "raw_faces": 0,
                    "raw_format": "simulated"}
            e.update(state="succeeded", result=raw, meta=meta)
            return
        glb, meta = self._export(e["body"], **e["params"])
        e.update(state="succeeded", result=glb, meta=meta)

    @staticmethod
    def _export(raw: bytes, exporter: str, decimation_target: int, texture_size: int,
                remesh: bool) -> tuple[bytes, dict[str, Any]]:
        import numpy as np
        import trimesh

        sphere = trimesh.creation.icosphere(subdivisions=2 if decimation_target < 1000 else 3)
        uv = np.stack([np.arctan2(sphere.vertices[:, 1], sphere.vertices[:, 0]) / (2 * np.pi) + 0.5,
                       sphere.vertices[:, 2] * 0.5 + 0.5], -1)
        shade = raw[14] if len(raw) > 14 else 128
        tex = Image.new("RGB", (64, 64), (shade, 120, 200 - shade // 2))
        sphere.visual = trimesh.visual.TextureVisuals(
            uv=uv, material=trimesh.visual.material.PBRMaterial(baseColorTexture=tex))
        return sphere.export(file_type="glb"), {"exporter": exporter, "faces_out": int(len(sphere.faces)),
                                                "decimation_target": decimation_target, "texture_size": 64,
                                                "remesh": remesh, "simulated": True,
                                                "licence": "SIMULATED", "limitations": []}
