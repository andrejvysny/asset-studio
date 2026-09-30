"""ComfyUI execution adapter: versioned graph + node-id bindings -> /prompt, /history, /queue, /view, /interrupt."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
from assetstudio_core.canonical import sha256_bytes, sha256_json
from assetstudio_core.safeyaml import load_yaml
from assetstudio_processing.images import ImageRejected, inspect_image

from .base import EngineImageHandle, EngineRejected, EngineUnavailable, ImageEditRequest, JobStatus, T2IRequest

TIMEOUT = httpx.Timeout(30.0, connect=5.0)
INPUT_SUBFOLDER = "assetstudio"


class Workflow:
    def __init__(self, workflows_dir: Path, bindings_file: str) -> None:
        self.bindings: dict[str, Any] = load_yaml((workflows_dir / bindings_file).read_bytes())
        self.graph: dict[str, Any] = json.loads((workflows_dir / self.bindings["graph"]).read_text())
        self.id: str = self.bindings["id"]
        self.version: int = int(self.bindings["version"])
        self.kind: str = self.bindings.get("kind", "t2i")
        self.conditioning: list[dict[str, str]] = list(self.bindings.get("conditioning") or [])
        self._check_static()

    def _check_static(self) -> None:
        for name, b in {**self.bindings["inputs"], **self.bindings["optional"]}.items():
            if b["node"] not in self.graph:
                raise ValueError(f"binding {name}: node {b['node']} not in graph")
        for name, b in self.bindings["inputs"].items():
            if b["input"] not in self.graph[b["node"]]["inputs"]:
                raise ValueError(f"binding {name}: node {b['node']} has no input {b['input']}")
        _assert_conditioning(self.graph, self.conditioning)

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

    def build_edit(self, req: ImageEditRequest, handle: EngineImageHandle) -> dict[str, Any]:
        if self.kind != "image_edit":
            raise ValueError(f"workflow {self.id} is not an image_edit workflow")
        g = copy.deepcopy(self.graph)
        values = {"positive": req.positive, "negative": req.negative, "seed": req.seed, "steps": req.steps,
                  "cfg": req.cfg, "filename_prefix": req.filename_prefix,
                  "source_image": f"{handle.subfolder}/{handle.name}"}
        for name, b in self.bindings["inputs"].items():
            g[b["node"]]["inputs"][b["input"]] = values[name]
        _assert_conditioning(g, self.conditioning)
        return g

    def model_files(self) -> list[tuple[str, str, str]]:
        return [(m["node"], m["input"], self.graph[m["node"]]["inputs"][m["input"]]) for m in self.bindings["models"]]


def _assert_conditioning(g: dict[str, Any], edges: list[dict[str, str]]) -> None:
    for e in edges:
        if e["from"] not in g or g.get(e["to"], {}).get("inputs", {}).get(e["input"]) != [e["from"], 0]:
            raise ValueError(f"source image not connected to conditioning: {e['from']} -> {e['to']}.{e['input']}")


def graph_sha256(g: dict[str, Any]) -> str:
    return sha256_json(g)


class WorkflowRegistry:
    def __init__(self, workflows_dir: Path) -> None:
        self.workflows: dict[str, Workflow] = {}
        for f in sorted(workflows_dir.glob("*.bindings.yaml")):
            wf = Workflow(workflows_dir, f.name)
            if wf.id in self.workflows:
                raise ValueError(f"duplicate workflow id {wf.id}")
            self.workflows[wf.id] = wf

    def get(self, workflow_id: str) -> Workflow:
        try:
            return self.workflows[workflow_id]
        except KeyError:
            raise EngineRejected(f"unknown workflow {workflow_id}") from None

    def by_kind(self, kind: str) -> list[Workflow]:
        return [w for w in self.workflows.values() if w.kind == kind]


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

    def __init__(self, base_url: str, workflows: Workflow | WorkflowRegistry,
                 client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        if isinstance(workflows, Workflow):
            reg = WorkflowRegistry.__new__(WorkflowRegistry)
            reg.workflows = {workflows.id: workflows}
            workflows = reg
        self.registry: WorkflowRegistry = workflows
        self.http = client or httpx.Client(base_url=self.base_url, timeout=TIMEOUT)
        self._prompt_workflow: dict[str, str] = {}  # lost on restart: callers persist the receipt's workflow id

    @property
    def workflow(self) -> Workflow:
        """The text-to-image workflow (backwards-compatible accessor)."""
        found = self.registry.by_kind("t2i")
        if not found:
            raise EngineRejected("no t2i workflow registered")
        return found[0]

    def supports(self, kind: str) -> bool:
        return bool(self.registry.by_kind(kind))

    def _get(self, path: str, **kw: Any) -> httpx.Response:
        try:
            r = self.http.get(path, **kw)
        except httpx.HTTPError as e:
            raise EngineUnavailable(f"ComfyUI unreachable: {type(e).__name__}") from e
        if r.status_code >= 500:
            raise EngineUnavailable(f"ComfyUI HTTP {r.status_code}")
        return r

    def _check_workflow(self, wf: Workflow, info: dict[str, Any]) -> list[str]:
        problems: list[str] = []
        for c in sorted({n["class_type"] for n in wf.graph.values()}):
            if info.get(c) is None:
                problems.append(f"node type {c} not available")
        for name, b in wf.bindings["inputs"].items():
            spec = info.get(wf.graph[b["node"]]["class_type"]) or {}
            req = {**spec.get("input", {}).get("required", {}), **spec.get("input", {}).get("optional", {})}
            if spec and b["input"] not in req:
                problems.append(f"{name}: input {b['input']} unknown to engine")
        for node, inp, filename in wf.model_files():
            spec = info.get(wf.graph[node]["class_type"]) or {}
            options = (spec.get("input", {}).get("required", {}).get(inp) or [[]])[0]
            if isinstance(options, list) and filename not in options:
                problems.append(f"model file {filename} not visible to ComfyUI")
        return problems

    def check(self) -> dict[str, Any]:
        """Validate every registered workflow against the running engine's node schemas and model lists."""
        wfs = list(self.registry.workflows.values())
        classes = sorted({n["class_type"] for w in wfs for n in w.graph.values()})
        try:
            info = {c: self._get(f"/object_info/{c}").json().get(c) for c in classes}
            stats = self._get("/system_stats").json()
        except (EngineUnavailable, ValueError) as e:
            return {"reachable": False, "ready": False, "problems": [str(e)], "workflows": {}}
        per = {w.id: self._check_workflow(w, info) for w in wfs}
        report = {w.id: {"kind": w.kind, "ready": not per[w.id], "problems": per[w.id]} for w in wfs}
        t2i = self.registry.by_kind("t2i")
        primary = per[t2i[0].id] if t2i else [p for ps in per.values() for p in ps]
        return {"reachable": True, "ready": not primary, "problems": primary, "workflows": report,
                "version": (stats.get("system") or {}).get("comfyui_version"),
                "devices": [{"name": d.get("name"), "vram_total": d.get("vram_total"), "vram_free": d.get("vram_free")}
                            for d in stats.get("devices", [])]}

    def lora_files(self) -> list[str]:
        spec = self._get("/object_info/LoraLoaderModelOnly").json().get("LoraLoaderModelOnly", {})
        opts = spec.get("input", {}).get("required", {}).get("lora_name", [[]])[0]
        return list(opts) if isinstance(opts, list) else []

    def _post_prompt(self, graph: dict[str, Any], prompt_id: str) -> None:
        try:
            r = self.http.post("/prompt", json={"prompt": graph, "prompt_id": prompt_id, "client_id": "assetstudio"})
        except httpx.HTTPError as e:
            raise EngineUnavailable(f"submit outcome unknown: {type(e).__name__}") from e
        if r.status_code == 400:
            raise EngineRejected(r.text[:500])
        if r.status_code >= 300:
            raise EngineUnavailable(f"submit HTTP {r.status_code}")
        body = r.json()
        if body.get("prompt_id") != prompt_id:
            raise EngineRejected(f"engine assigned a different prompt id {body.get('prompt_id')}")

    def submit(self, req: T2IRequest) -> str:
        wf = self.workflow
        self._post_prompt(wf.build(req), req.prompt_id)
        self._prompt_workflow[req.prompt_id] = wf.id
        return req.prompt_id

    def _view_input(self, name: str, subfolder: str) -> bytes | None:
        r = self._get("/view", params={"filename": name, "subfolder": subfolder, "type": "input"})
        return r.content if r.status_code == 200 else None

    def upload_input(self, image: bytes) -> EngineImageHandle:
        try:
            inspect_image(image, allowed=("PNG",))
        except ImageRejected as e:
            raise EngineRejected(f"source image rejected: {e}") from e
        digest = sha256_bytes(image)
        name = f"as_{digest[:32]}.png"
        try:
            r = self.http.post("/upload/image", files={"image": (name, image, "image/png")},
                               data={"overwrite": "false", "type": "input", "subfolder": INPUT_SUBFOLDER})
        except httpx.HTTPError as e:
            raise EngineUnavailable(f"upload outcome unknown: {type(e).__name__}") from e
        if r.status_code == 400:
            raise EngineRejected(f"upload refused: {r.text[:300]}")
        if r.status_code >= 300:
            raise EngineUnavailable(f"upload HTTP {r.status_code}")
        try:
            body = r.json()
            got_name, got_sub = str(body["name"]), str(body.get("subfolder", ""))
        except (ValueError, KeyError, TypeError) as e:
            raise EngineRejected("upload response malformed") from e
        if got_sub != INPUT_SUBFOLDER:
            raise EngineRejected(f"input stored in unexpected subfolder {got_sub!r}")
        if got_name != name:  # the name existed: only reuse it when the stored bytes are identical
            existing = self._view_input(name, INPUT_SUBFOLDER)
            if existing is None or sha256_bytes(existing) != digest:
                raise EngineRejected("input name collision")
        return EngineImageHandle(name=name, subfolder=INPUT_SUBFOLDER, sha256=digest)

    def _edit_workflow(self) -> Workflow:
        found = self.registry.by_kind("image_edit")
        if not found:
            raise EngineRejected("no image_edit workflow registered")
        return found[0]

    def submit_edit(self, req: ImageEditRequest) -> dict[str, Any]:
        if sha256_bytes(req.image) != req.prepared_input_sha256:
            raise EngineRejected("prepared input hash mismatch")
        wf = self._edit_workflow()
        handle = self.upload_input(req.image)
        try:
            graph = wf.build_edit(req, handle)
        except ValueError as e:
            raise EngineRejected(str(e)) from e
        self._post_prompt(graph, req.prompt_id)
        self._prompt_workflow[req.prompt_id] = wf.id
        return {"prompt_id": req.prompt_id, "workflow": wf.id, "workflow_version": wf.version,
                "graph_sha256": graph_sha256(graph),
                "input": {"name": handle.name, "subfolder": handle.subfolder, "sha256": handle.sha256}}

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

    def fetch_image(self, prompt_id: str, workflow_id: str | None = None) -> bytes:
        hist = self._get(f"/history/{prompt_id}").json().get(prompt_id) or {}
        wf_id = workflow_id or self._prompt_workflow.get(prompt_id)
        wf = self.registry.get(wf_id) if wf_id else self.workflow
        node = wf.bindings["outputs"]["image"]["node"]
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
                "workflow_version": self.workflow.version, "workflows": list(self.registry.workflows)}
