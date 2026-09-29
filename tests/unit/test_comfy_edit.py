from __future__ import annotations

import copy
import io
import json
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import httpx
import pytest
from assetstudio_core.canonical import sha256_bytes, sha256_json
from assetstudio_server.adapters.base import (
    EngineImageHandle,
    EngineRejected,
    EngineUnavailable,
    ImageEditRequest,
    LoraUse,
    T2IRequest,
)
from assetstudio_server.adapters.comfyui import ComfyEngine, Workflow, WorkflowRegistry
from assetstudio_server.adapters.fake import FakeEngine
from PIL import Image

WF_DIR = Path(__file__).resolve().parents[2] / "comfyui" / "workflows"
EDIT_ID, T2I_ID = "comfyui.qwen_edit_2511", "comfyui.qwen_t2i"


def png(color: tuple[int, int, int], size: int = 32) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (size, size), color).save(out, "PNG")
    return out.getvalue()


def edit_req(image: bytes, **kw: Any) -> ImageEditRequest:
    base: dict[str, Any] = dict(prompt_id="22222222-2222-5222-8222-222222222222", source_sha256="a" * 64,
                                prepared_input_sha256=sha256_bytes(image), image=image, positive="shorter",
                                negative="", seed=7, steps=40, cfg=4.0, filename_prefix="edit")
    base.update(kw)
    return ImageEditRequest(**base)


def make_engine(handler: Any) -> ComfyEngine:
    return ComfyEngine("http://c", WorkflowRegistry(WF_DIR),
                       httpx.Client(transport=httpx.MockTransport(handler), base_url="http://c"))


def test_registry_and_static_conditioning_validation(tmp_path: Path) -> None:
    reg = WorkflowRegistry(WF_DIR)
    assert set(reg.workflows) == {T2I_ID, EDIT_ID}
    assert [w.id for w in reg.by_kind("t2i")] == [T2I_ID] and [w.id for w in reg.by_kind("image_edit")] == [EDIT_ID]
    for f in WF_DIR.glob("image.edit.qwen2511.*"):
        (tmp_path / f.name).write_bytes(f.read_bytes())
    g = json.loads((tmp_path / "image.edit.qwen2511.api.json").read_text())
    g["8"]["inputs"]["image1"] = ["6", 0]  # bypasses the scale node
    (tmp_path / "image.edit.qwen2511.api.json").write_text(json.dumps(g))
    with pytest.raises(ValueError, match="not connected to conditioning"):
        Workflow(tmp_path, "image.edit.qwen2511.bindings.yaml")


def test_build_edit_connects_handle_and_asserts_edges() -> None:
    wf = WorkflowRegistry(WF_DIR).get(EDIT_ID)
    h = EngineImageHandle("as_x.png", "assetstudio", "0" * 64)
    g = wf.build_edit(edit_req(png((1, 2, 3))), h)
    assert g["6"]["inputs"]["image"] == "assetstudio/as_x.png"
    assert g["13"]["inputs"]["seed"] == 7 and g["8"]["inputs"]["prompt"] == "shorter"
    for e in wf.conditioning:
        assert g[e["to"]]["inputs"][e["input"]] == [e["from"], 0]
    broken = copy.deepcopy(wf)
    broken.graph["7"]["inputs"]["image"] = ["1", 0]  # LoadImage present but disconnected
    with pytest.raises(ValueError, match="source image not connected to conditioning"):
        broken.build_edit(edit_req(png((1, 2, 3))), h)


def test_t2i_graph_unchanged_regression() -> None:
    wf = WorkflowRegistry(WF_DIR).get(T2I_ID)
    r = T2IRequest(prompt_id="p", positive="pos", negative="neg", seed=7, width=64, height=64, steps=4, cfg=1.0,
                   filename_prefix="x")
    assert sha256_json(wf.build(r)) == "ca453c14cc35c819070adc8f393fd62fd0ac4b6981bf5667c48c8cd74ca032d8"
    r2 = T2IRequest(**{**r.__dict__, "speed_lora": LoraUse("fast.safetensors", 1.0),
                       "style_lora": LoraUse("s.safetensors", 0.5)})
    assert sha256_json(wf.build(r2)) == "400ab67f7c353446b3e6654e34dcb1627d3dbef369713835d6ec5556685a7750"


class Server:
    """Minimal ComfyUI stand-in: an input store with rename-on-collision like the real /upload/image."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.uploads: list[dict[str, str]] = []
        self.prompts: list[dict[str, Any]] = []
        self.history: dict[str, Any] = {}
        self.prompt_status = 200
        self.fail_transport = False

    def __call__(self, r: httpx.Request) -> httpx.Response:
        if self.fail_transport:
            raise httpx.ConnectError("down")
        if r.url.path == "/upload/image":
            msg = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + r.headers["content-type"].encode() + b"\r\n\r\n" + r.content)
            fields = {p.get_param("name", header="content-disposition"): p for p in msg.iter_parts()}
            name, payload = fields["image"].get_filename(), fields["image"].get_payload(decode=True)
            self.uploads.append({k: v.get_content() for k, v in fields.items() if k != "image"})
            final = name
            if final in self.files:
                final = name.replace(".png", " (1).png")
            self.files[final] = payload
            return httpx.Response(200, json={"name": final, "subfolder": "assetstudio", "type": "input"})
        if r.url.path == "/view":
            fn = r.url.params["filename"]
            if r.url.params.get("type") == "input":
                return httpx.Response(200, content=self.files[fn]) if fn in self.files else httpx.Response(404)
            return httpx.Response(200, content=b"OUT:" + fn.encode())
        if r.url.path == "/prompt":
            if self.prompt_status != 200:
                return httpx.Response(self.prompt_status, text="bad graph")
            body = json.loads(r.content)
            self.prompts.append(body)
            return httpx.Response(200, json={"prompt_id": body["prompt_id"]})
        if r.url.path.startswith("/history/"):
            return httpx.Response(200, json=self.history)
        return httpx.Response(200, json={})


def test_upload_content_named_reuse_and_overwrite_false() -> None:
    srv = Server()
    eng = make_engine(srv)
    img = png((10, 20, 30))
    h1 = eng.upload_input(img)
    assert h1.name == f"as_{sha256_bytes(img)[:32]}.png" and h1.subfolder == "assetstudio"
    h2 = eng.upload_input(img)  # server renamed to "(1)"; identical bytes -> canonical name reused
    assert h2 == h1 and len(srv.files) == 2
    assert all(u["overwrite"] == "false" and u["subfolder"] == "assetstudio" for u in srv.uploads)


def test_upload_collision_and_non_png_rejected() -> None:
    srv = Server()
    eng = make_engine(srv)
    img = png((10, 20, 30))
    srv.files[f"as_{sha256_bytes(img)[:32]}.png"] = b"different bytes"
    with pytest.raises(EngineRejected, match="input name collision"):
        eng.upload_input(img)
    jpg = io.BytesIO()
    Image.new("RGB", (8, 8)).save(jpg, "JPEG")
    with pytest.raises(EngineRejected):
        eng.upload_input(jpg.getvalue())
    with pytest.raises(EngineRejected):
        eng.upload_input(b"garbage")


def test_submit_edit_receipt_and_errors() -> None:
    srv = Server()
    eng = make_engine(srv)
    img = png((5, 5, 5))
    rec = eng.submit_edit(edit_req(img))
    assert rec["workflow"] == EDIT_ID and rec["input"]["sha256"] == sha256_bytes(img)
    sent = srv.prompts[0]
    assert sent["prompt_id"] == rec["prompt_id"] and sha256_json(sent["prompt"]) == rec["graph_sha256"]
    assert sent["prompt"]["6"]["inputs"]["image"] == f"assetstudio/{rec['input']['name']}"
    with pytest.raises(EngineRejected, match="prepared input hash mismatch"):
        eng.submit_edit(edit_req(img, prepared_input_sha256="0" * 64))
    srv.prompt_status = 400
    with pytest.raises(EngineRejected):
        eng.submit_edit(edit_req(img))
    srv.prompt_status = 200
    srv.fail_transport = True
    with pytest.raises(EngineUnavailable):
        eng.submit_edit(edit_req(png((6, 6, 6))))


def test_fetch_image_uses_workflow_output_node() -> None:
    srv = Server()
    eng = make_engine(srv)
    srv.history = {"pid": {"outputs": {"15": {"images": [{"filename": "edit_1.png", "type": "output"}]},
                                       "12": {"images": [{"filename": "t2i_1.png", "type": "output"}]}}}}
    assert eng.fetch_image("pid", workflow_id=EDIT_ID) == b"OUT:edit_1.png"
    assert eng.fetch_image("pid", workflow_id=T2I_ID) == b"OUT:t2i_1.png"
    eng.submit_edit(edit_req(png((5, 5, 5)), prompt_id="pid"))
    assert eng.fetch_image("pid") == b"OUT:edit_1.png"  # map populated by submit
    assert eng.supports("image_edit") and eng.supports("t2i") and not eng.supports("x")


def test_fake_engine_edit_depends_on_source() -> None:
    def run(img: bytes, seed: int) -> bytes:
        eng = FakeEngine()
        rec = eng.submit_edit(edit_req(img, seed=seed))
        return eng.fetch_image(rec["prompt_id"])

    a, b = png((200, 30, 30), 64), png((30, 30, 200), 64)
    assert run(a, 7) == run(a, 7)
    assert run(a, 7) != run(b, 7) and run(a, 7) != run(a, 8)
    with pytest.raises(EngineRejected):
        FakeEngine().submit_edit(edit_req(a, prepared_input_sha256="0" * 64))
