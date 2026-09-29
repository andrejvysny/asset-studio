"""ComfyUI execution adapter: versioned graph + node-id bindings -> /prompt, /history, /queue, /view, /interrupt."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
from assetstudio_core.safeyaml import load_yaml

from .base import EngineRejected, EngineUnavailable, JobStatus, T2IRequest

TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class Workflow:
    def __init__(self, workflows_dir: Path, bindings_file: str) -> None:
        self.bindings: dict[str, Any] = load_yaml((workflows_dir / bindings_file).read_bytes())
        self.graph: dict[str, Any] = json.loads((workflows_dir / self.bindings["graph"]).read_text())
        self.id: str = self.bindings["id"]
        self.version: int = int(self.bindings["version"])
        self._check_static()

    def _check_static(self) -> None:
        for name, b in {**self.bindings["inputs"], **self.bindings["optional"]}.items():
            if b["node"] not in self.graph:
                raise ValueError(f"binding {name}: node {b['node']} not in graph")
        for name, b in self.bindings["inputs"].items():
            if b["input"] not in self.graph[b["node"]]["inputs"]:
                raise ValueError(f"binding {name}: node {b['node']} has no input {b['input']}")

    def build(self, req: T2IRequest) -> dict[str, Any]:
        g = copy.deepcopy(self.graph)
        values = {"positive": req.positive, "negative": req.negative, "seed": req.seed, "steps": req.steps,
                  "cfg": req.cfg, "width": req.width, "height": req.height, "filename_prefix": req.filename_prefix}
        for name, b in self.bindings["inputs"].items():
            g[b["node"]]["inputs"][b["input"]] = values[name]
        for name, use in (("style_lora", req.style_lora), ("speed_lora", req.speed_lora)):
            b = self.bindings["optional"][name]
            if use is None:
                _remove_passthrough(g, b["node"], b["passthrough"])
            else:
                g[b["node"]]["inputs"][b["inputs"]["file"]] = use.file
                g[b["node"]]["inputs"][b["inputs"]["strength"]] = use.strength
        return g

    def model_files(self) -> list[tuple[str, str, str]]:
        return [(m["node"], m["input"], self.graph[m["node"]]["inputs"][m["input"]]) for m in self.bindings["models"]]


def _remove_passthrough(g: dict[str, Any], node: str, via: str) -> None:
    """Drop an optional node and point its consumers at the node's own input (e.g. an unused LoRA loader)."""
    source = g[node]["inputs"][via]
    del g[node]
    for spec in g.values():
        for key, val in spec["inputs"].items():
            if isinstance(val, list) and len(val) == 2 and val[0] == node:
                spec["inputs"][key] = list(source)


class ComfyEngine:
    name = "comfyui"
    simulated = False

    def __init__(self, base_url: str, workflow: Workflow, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.workflow = workflow
        self.http = client or httpx.Client(base_url=self.base_url, timeout=TIMEOUT)

    def _get(self, path: str, **kw: Any) -> httpx.Response:
        try:
            r = self.http.get(path, **kw)
        except httpx.HTTPError as e:
            raise EngineUnavailable(f"ComfyUI unreachable: {type(e).__name__}") from e
        if r.status_code >= 500:
            raise EngineUnavailable(f"ComfyUI HTTP {r.status_code}")
        return r

    def check(self) -> dict[str, Any]:
        """Validate bindings against the running engine's node schemas and model lists."""
        problems: list[str] = []
        classes = sorted({n["class_type"] for n in self.workflow.graph.values()})
        try:
            info = {c: self._get(f"/object_info/{c}").json().get(c) for c in classes}
            stats = self._get("/system_stats").json()
        except (EngineUnavailable, ValueError) as e:
            return {"reachable": False, "ready": False, "problems": [str(e)]}
        for c, spec in info.items():
            if spec is None:
                problems.append(f"node type {c} not available")
        for name, b in self.workflow.bindings["inputs"].items():
            spec = info.get(self.workflow.graph[b["node"]]["class_type"]) or {}
            req = {**spec.get("input", {}).get("required", {}), **spec.get("input", {}).get("optional", {})}
            if spec and b["input"] not in req:
                problems.append(f"{name}: input {b['input']} unknown to engine")
        for node, inp, filename in self.workflow.model_files():
            spec = info.get(self.workflow.graph[node]["class_type"]) or {}
            options = (spec.get("input", {}).get("required", {}).get(inp) or [[]])[0]
            if isinstance(options, list) and filename not in options:
                problems.append(f"model file {filename} not visible to ComfyUI")
        return {"reachable": True, "ready": not problems, "problems": problems,
                "version": (stats.get("system") or {}).get("comfyui_version"),
                "devices": [{"name": d.get("name"), "vram_total": d.get("vram_total"), "vram_free": d.get("vram_free")}
                            for d in stats.get("devices", [])]}

    def lora_files(self) -> list[str]:
        spec = self._get("/object_info/LoraLoaderModelOnly").json().get("LoraLoaderModelOnly", {})
        opts = spec.get("input", {}).get("required", {}).get("lora_name", [[]])[0]
        return list(opts) if isinstance(opts, list) else []

    def submit(self, req: T2IRequest) -> str:
        graph = self.workflow.build(req)
        try:
            r = self.http.post("/prompt", json={"prompt": graph, "prompt_id": req.prompt_id,
                                                "client_id": "assetstudio"})
        except httpx.HTTPError as e:
            raise EngineUnavailable(f"submit outcome unknown: {type(e).__name__}") from e
        if r.status_code == 400:
            raise EngineRejected(r.text[:500])
        if r.status_code >= 300:
            raise EngineUnavailable(f"submit HTTP {r.status_code}")
        body = r.json()
        if body.get("prompt_id") != req.prompt_id:
            raise EngineRejected(f"engine assigned a different prompt id {body.get('prompt_id')}")
        return req.prompt_id

    def status(self, prompt_id: str) -> JobStatus:
        hist = self._get(f"/history/{prompt_id}").json().get(prompt_id)
        if hist:
            st = hist.get("status") or {}
            if st.get("status_str") == "success" and st.get("completed"):
                return JobStatus("succeeded")
            if st.get("status_str") == "error":
                msgs = [m for m in st.get("messages", []) if m and m[0] == "execution_error"]
                err = msgs[0][1].get("exception_message", "execution error") if msgs else "execution error"
                return JobStatus("failed", str(err)[:500])
            return JobStatus("running")
        q = self._get("/queue").json()
        if any(item[1] == prompt_id for item in q.get("queue_running", [])):
            return JobStatus("running")
        if any(item[1] == prompt_id for item in q.get("queue_pending", [])):
            return JobStatus("pending")
        return JobStatus("unknown")

    def fetch_image(self, prompt_id: str) -> bytes:
        hist = self._get(f"/history/{prompt_id}").json().get(prompt_id) or {}
        node = self.workflow.bindings["outputs"]["image"]["node"]
        images = ((hist.get("outputs") or {}).get(node) or {}).get("images") or []
        if not images:
            raise EngineRejected(f"no image output from registered node {node}")
        ref = images[0]
        r = self._get("/view", params={"filename": ref["filename"], "subfolder": ref.get("subfolder", ""),
                                       "type": ref.get("type", "output")})
        if r.status_code != 200:
            raise EngineUnavailable(f"/view HTTP {r.status_code}")
        return r.content

    def cancel(self, prompt_id: str) -> dict[str, Any]:
        """Dequeue by id; interrupt only if this exact prompt is running (never a global interrupt)."""
        try:
            self.http.post("/queue", json={"delete": [prompt_id]})
            self.http.post("/interrupt", json={"prompt_id": prompt_id})
        except httpx.HTTPError as e:
            raise EngineUnavailable(f"cancel outcome unknown: {type(e).__name__}") from e
        return {"requested": True}

    def describe(self) -> dict[str, Any]:
        return {"engine": self.name, "url": self.base_url, "workflow": self.workflow.id,
                "workflow_version": self.workflow.version}
