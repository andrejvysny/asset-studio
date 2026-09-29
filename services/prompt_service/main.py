"""aux service (GPU1, time-shared): Qwen3-VL enhancement + advisory QA, BiRefNet segmentation.

Stateless inference only: receives bytes/text, returns results. Owns no product state and never writes files.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
from pathlib import Path

import numpy as np
import torch
from fastapi import FastAPI, HTTPException
from lazy_model import LazyModel, gpu_info
from PIL import Image
from pydantic import BaseModel, Field, ValidationError

MODELS = Path(os.environ.get("MODELS_ROOT", "/models"))
VLM_DIR = MODELS / "checkpoints" / "qwen3-vl-8b-instruct"
BIREFNET_DIR = MODELS / "checkpoints" / "birefnet"
PROMPTS = Path(os.environ.get("PROMPTS_DIR", "/config/prompts"))
IDLE_UNLOAD_S = float(os.environ.get("IDLE_UNLOAD_S", "300"))
MAX_IMAGE_PIXELS = 40_000_000


def _load_vlm() -> tuple[object, object]:
    from transformers import AutoModelForImageTextToText, AutoProcessor

    model = AutoModelForImageTextToText.from_pretrained(VLM_DIR, dtype=torch.bfloat16, device_map="cuda:0",
                                                        local_files_only=True)
    return model, AutoProcessor.from_pretrained(VLM_DIR, local_files_only=True)


def _load_birefnet() -> torch.nn.Module:
    from transformers import AutoModelForImageSegmentation

    # trust_remote_code: executes the audited, hash-pinned birefnet.py from the local model dir only.
    model = AutoModelForImageSegmentation.from_pretrained(str(BIREFNET_DIR), trust_remote_code=True,
                                                          local_files_only=True)
    return model.eval().half().cuda()


vlm = LazyModel(_load_vlm, IDLE_UNLOAD_S)
birefnet = LazyModel(_load_birefnet, IDLE_UNLOAD_S)
app = FastAPI(title="assetstudio aux")


def _image(b64: str) -> Image.Image:
    try:
        raw = base64.b64decode(b64, validate=True)
        im = Image.open(io.BytesIO(raw))
        if im.size[0] * im.size[1] > MAX_IMAGE_PIXELS:
            raise HTTPException(400, "image too large")
        im.load()
        return im.convert("RGB")
    except (ValueError, OSError) as e:
        raise HTTPException(400, f"invalid image: {e}") from e


def _generate(messages: list[dict], max_new_tokens: int) -> str:
    with vlm.use() as (model, processor):
        inputs = processor.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,  # type: ignore
                                               return_dict=True, return_tensors="pt").to(model.device)  # type: ignore
        with torch.inference_mode():
            out = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)  # type: ignore
        return processor.batch_decode(out[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)[0]  # type: ignore


def _json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in model output")
    return json.loads(match.group(0))


def _generate_json(messages: list[dict], schema: type[BaseModel], max_new_tokens: int) -> tuple[BaseModel, str]:
    """Greedy decode + schema validation; one retry with the error fed back, then a 502 (never a fake answer)."""
    raw = _generate(messages, max_new_tokens)
    try:
        return schema.model_validate(_json(raw)), raw
    except (ValueError, ValidationError) as e:
        retry = messages + [{"role": "assistant", "content": [{"type": "text", "text": raw}]},
                            {"role": "user", "content": [{"type": "text",
                                                         "text": f"Invalid output ({e}). Reply with JSON only."}]}]
        raw = _generate(retry, max_new_tokens)
        try:
            return schema.model_validate(_json(raw)), raw
        except (ValueError, ValidationError) as e2:
            raise HTTPException(502, f"model returned invalid JSON twice: {str(e2)[:200]}") from e2


class EnhanceRequest(BaseModel):
    brief: str = Field(min_length=1, max_length=4000)
    kind: str = Field(max_length=60)
    constraints: str = Field(default="", max_length=2000)
    style_guide: str = Field(default="", max_length=8000)


class EnhanceResult(BaseModel):
    description: str = Field(min_length=1)
    short_title: str = ""
    tags: list[str] = []


@app.post("/enhance")
def enhance(req: EnhanceRequest) -> dict:
    tpl = (PROMPTS / "system_prompt_enhancer.txt").read_text()
    system = tpl.format(kind=req.kind, constraints=req.constraints or "(none)", style_guide=req.style_guide or "(none)")
    messages = [{"role": "system", "content": [{"type": "text", "text": system}]},
                {"role": "user", "content": [{"type": "text", "text": req.brief}]}]
    t0 = time.monotonic()
    result, raw = _generate_json(messages, EnhanceResult, 512)
    return {**result.model_dump(), "meta": {"model": "Qwen3-VL-8B-Instruct", "seconds": round(time.monotonic() - t0, 2),
                                            "raw": raw}}


class Question(BaseModel):
    id: str = Field(max_length=64)
    question: str = Field(max_length=500)


class QARequest(BaseModel):
    image_b64: str
    questions: list[Question] = Field(min_length=1, max_length=64)
    context: str = Field(default="", max_length=4000)


@app.post("/qa")
def qa(req: QARequest) -> dict:
    image = _image(req.image_b64)
    checks = "\n".join(f"- {q.id}: {q.question}" for q in req.questions)
    system = (PROMPTS / "system_prompt_qa.txt").read_text().format(checks=checks)
    user = "Inspect this image." + (f" It was generated for: {req.context}" if req.context else "")
    messages = [{"role": "system", "content": [{"type": "text", "text": system}]},
                {"role": "user", "content": [{"type": "image", "image": image}, {"type": "text", "text": user}]}]
    t0 = time.monotonic()
    raw = _generate(messages, 768)
    try:
        body = _json(raw)
    except ValueError:
        body = {}
    # Values are passed through untouched: the Studio validates strictly (a string "false" is not a boolean).
    return {"checks": body.get("checks") if isinstance(body, dict) else None,
            "reasons": body.get("reasons", []) if isinstance(body, dict) else [],
            "summary": body.get("summary", "") if isinstance(body, dict) else "",
            "meta": {"model": "Qwen3-VL-8B-Instruct", "seconds": round(time.monotonic() - t0, 2), "raw": raw[:2000]}}


class CutoutRequest(BaseModel):
    image_b64: str


@app.post("/cutout")
def cutout(req: CutoutRequest) -> dict:
    from torchvision import transforms

    image = _image(req.image_b64)
    tf = transforms.Compose([transforms.Resize((1024, 1024)), transforms.ToTensor(),
                             transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])])
    with birefnet.use() as model:
        x = tf(image).unsqueeze(0).cuda().half()
        with torch.inference_mode():
            pred = model(x)[-1].sigmoid()[0, 0].float().cpu().numpy()
    mask = Image.fromarray((pred * 255).astype(np.uint8)).resize(image.size, Image.Resampling.BILINEAR)
    out = io.BytesIO()
    mask.save(out, "PNG")
    return {"mask_b64": base64.b64encode(out.getvalue()).decode(), "meta": {"model": "BiRefNet"}}


@app.get("/health")
def health() -> dict:
    return {"ok": True, "models_present": {"vlm": (VLM_DIR / "config.json").is_file(),
                                           "birefnet": (BIREFNET_DIR / "config.json").is_file()},
            "loaded": {"vlm": vlm.loaded, "birefnet": birefnet.loaded}, "loads": {"vlm": vlm.loads,
                                                                                  "birefnet": birefnet.loads},
            "cuda": torch.cuda.is_available(), "gpu": gpu_info()}


class UnloadRequest(BaseModel):
    owner_token: str = Field(min_length=1, max_length=100)


@app.post("/unload")
def unload(req: UnloadRequest) -> dict:
    """Acknowledges release only after both models are really gone (waits for in-flight requests)."""
    if not (vlm.unload() and birefnet.unload()):
        raise HTTPException(409, "models still in use")
    return {"loaded": vlm.loaded or birefnet.loaded, "owner_token": req.owner_token}
