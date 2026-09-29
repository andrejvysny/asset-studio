"""aux service (GPU1, time-shared): Qwen3-VL enhance/QA/compare/analysis/variant suggestions, BiRefNet segmentation.

Stateless inference only: receives bytes/text, returns results. Owns no product state and never writes files.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import aux_v2
import numpy as np
import torch
from fastapi import Depends, FastAPI, Header, HTTPException
from lazy_model import LazyModel, gpu_info
from lease import Lease, StaleLease
from PIL import Image
from pydantic import BaseModel, Field, ValidationError

MODELS = Path(os.environ.get("MODELS_ROOT", "/models"))
VLM_DIR = MODELS / "checkpoints" / "qwen3-vl-8b-instruct"
BIREFNET_DIR = MODELS / "checkpoints" / "birefnet"
PROMPTS = Path(os.environ.get("PROMPTS_DIR", "/config/prompts"))
IDLE_UNLOAD_S = float(os.environ.get("IDLE_UNLOAD_S", "300"))
MAX_IMAGE_PIXELS = 40_000_000
MAX_VLM_SIDE = 1024
MODEL_NAME = "Qwen3-VL-8B-Instruct"


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
lease = Lease()
app = FastAPI(title="assetstudio aux")


def gpu_work(x_lease_epoch: str | None = Header(default=None),
             x_execution_id: str | None = Header(default=None, max_length=80)):  # noqa: ANN201 - dependency
    """Every GPU request runs inside the lease: counted for /unload, refused without the admitted epoch."""
    try:
        lease.enter(int(x_lease_epoch) if x_lease_epoch and x_lease_epoch.isdigit() else None)
    except StaleLease as e:
        raise HTTPException(409, f"stale_lease: {e}") from e
    try:
        yield x_execution_id
    finally:
        lease.leave()


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
    preset: Literal["conservative", "creative"] = "conservative"
    mode: Literal["t2i", "edit"] = "t2i"
    images: list[aux_v2.RefImage] = Field(default=[], max_length=4)
    preserve: str = Field(default="", max_length=2000)
    change: str = Field(default="", max_length=2000)


class EnhanceResult(BaseModel):
    description: str = Field(min_length=1)
    short_title: str = ""
    tags: list[str] = []


PRESET_RULES = {
    "conservative": "CONSERVATIVE: use only facts stated in the brief. Do not add details, parts or features. "
                    'Set "additions" to []. Put anything you had to assume in "assumptions".',
    "creative": "CREATIVE: you may add fitting details, but list EVERY invented detail separately in \"additions\" "
                "(they are reviewed by a human) and never contradict the brief.",
}


def _prep(b64: str) -> Image.Image:
    im = _image(b64)
    if max(im.size) > MAX_VLM_SIDE:
        im.thumbnail((MAX_VLM_SIDE, MAX_VLM_SIDE), Image.Resampling.LANCZOS)
    return im


def _meta(t0: float, execution_id: str | None) -> dict:
    return {"model": MODEL_NAME, "seconds": round(time.monotonic() - t0, 2), "execution_id": execution_id,
            "vlm_loads": vlm.loads}


def _generate_parsed(messages: list[dict], parse: Callable[[str], dict], max_new_tokens: int) -> tuple[dict, str]:
    """Like _generate_json for the v2 parsers: one retry with the error fed back, then 502."""
    raw = _generate(messages, max_new_tokens)
    try:
        return parse(raw), raw
    except ValueError as e:
        retry = messages + [{"role": "assistant", "content": [{"type": "text", "text": raw}]},
                            {"role": "user", "content": [{"type": "text",
                                                         "text": f"Invalid output ({e}). Reply with JSON only."}]}]
        raw = _generate(retry, max_new_tokens)
        try:
            return parse(raw), raw
        except ValueError as e2:
            raise HTTPException(502, f"model returned invalid output twice: {str(e2)[:200]}") from e2


def _sys(text: str) -> dict:
    return {"role": "system", "content": [{"type": "text", "text": text}]}


def _images_block(images: list[tuple[Image.Image, str]]) -> list[dict]:
    """Each image is preceded by its own text label so the model can cite it by number."""
    out: list[dict] = []
    for n, (im, label) in enumerate(images, 1):
        out += [{"type": "text", "text": f"Image {n} ({label}):"}, {"type": "image", "image": im}]
    return out


@app.post("/enhance")
def enhance(req: EnhanceRequest, execution_id: str | None = Depends(gpu_work)) -> dict:
    t0 = time.monotonic()
    legacy = req.preset == "conservative" and req.mode == "t2i" and not req.images and not req.preserve \
        and not req.change
    if legacy:  # old payloads behave exactly as before
        tpl = (PROMPTS / "system_prompt_enhancer.txt").read_text()
        system = tpl.format(kind=req.kind, constraints=req.constraints or "(none)",
                            style_guide=req.style_guide or "(none)")
        messages = [_sys(system), {"role": "user", "content": [{"type": "text", "text": req.brief}]}]
        result, raw = _generate_json(messages, EnhanceResult, 512)
        return {**result.model_dump(), "facts": [], "additions": [], "assumptions": [], "reference_cues": [],
                "meta": {**_meta(t0, execution_id), "raw": raw}}
    refs = [r for r in req.images if r.role == "reference"]
    sources = [r for r in req.images if r.role == "source"]
    ordered = (sources[:1] if req.mode == "edit" else []) + refs
    notes = "\n".join(f"{n}. {r.note or '(no note)'}" for n, r in enumerate(refs, 1)) or "(none)"
    fname = "system_prompt_enhancer_edit.txt" if req.mode == "edit" else "system_prompt_enhancer_v2.txt"
    system = (PROMPTS / fname).read_text().format(
        kind=req.kind, constraints=req.constraints or "(none)", style_guide=req.style_guide or "(none)",
        preset=req.preset, preset_rules=PRESET_RULES[req.preset], reference_notes=notes,
        change=req.change or req.brief, preserve=req.preserve or "(everything not named in the change)")
    labelled = [(_prep(r.b64), r.role) for r in ordered]
    n_src = 1 if req.mode == "edit" and sources else 0
    user = _images_block(labelled) + [{"type": "text", "text": req.brief}]
    messages = [_sys(system), {"role": "user", "content": user}]

    def parse(raw: str) -> dict:
        # prompt numbers references 1-based; output cue index is 0-based over the references
        out = aux_v2.parse_enhance_output(raw, req.preset)
        cues = []
        for c in out["reference_cues"]:
            if 1 <= c["index"] <= len(refs):
                cues.append({"index": c["index"] - 1, "cue": c["cue"]})
        return {**out, "reference_cues": cues}
    result, raw = _generate_parsed(messages, parse, 768)
    return {**result, "meta": {**_meta(t0, execution_id), "raw": raw[:4000], "edit_source_images": n_src}}


class Question(BaseModel):
    id: str = Field(max_length=64)
    question: str = Field(max_length=500)


class QARequest(BaseModel):
    image_b64: str
    questions: list[Question] = Field(min_length=1, max_length=64)
    context: str = Field(default="", max_length=4000)


@app.post("/qa")
def qa(req: QARequest, execution_id: str | None = Depends(gpu_work)) -> dict:
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
            "meta": {"model": "Qwen3-VL-8B-Instruct", "seconds": round(time.monotonic() - t0, 2), "raw": raw[:2000],
                     "execution_id": execution_id, "vlm_loads": vlm.loads}}


@app.post("/compare")
def compare(req: aux_v2.CompareRequest, execution_id: str | None = Depends(gpu_work)) -> dict:
    ids = [q.id for q in req.questions]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "duplicate question ids")
    images = [(_prep(i.b64), i.label + (f": {i.note}" if i.note else "")) for i in req.images]
    checks = "\n".join(f"- {q.id}: {q.question}" for q in req.questions)
    system = (PROMPTS / "system_prompt_compare.txt").read_text().format(checks=checks)
    text = "Compare the images." + (f" Context: {req.context}" if req.context else "")
    messages = [_sys(system), {"role": "user", "content": _images_block(images) + [{"type": "text", "text": text}]}]
    t0 = time.monotonic()
    raw = _generate(messages, 768)
    return {**aux_v2.parse_compare(raw, ids), "meta": {**_meta(t0, execution_id), "raw": raw[:2000]}}


@app.post("/analyze_source")
def analyze_source(req: aux_v2.AnalyzeRequest, execution_id: str | None = Depends(gpu_work)) -> dict:
    images = [(_prep(i.b64), i.view or "view") for i in req.images]
    system = (PROMPTS / "system_prompt_analyze_source.txt").read_text().format(
        kind=req.kind or "object", user_facts=req.user_facts or "(none)")
    messages = [_sys(system), {"role": "user", "content": _images_block(images)
                               + [{"type": "text", "text": "Analyse these views."}]}]
    t0 = time.monotonic()
    result, raw = _generate_parsed(messages, lambda r: aux_v2.parse_analysis(r, len(images)), 1024)
    return {**result, "meta": {**_meta(t0, execution_id), "raw": raw[:4000]}}


INTENT_RULES = {
    "subtle": "SUBTLE: small changes, every row stays very close to the source.",
    "related": "RELATED: clearly distinct siblings that keep the source identity.",
    "exploratory": "EXPLORATORY: a wider range of allowed attributes, but still keep every declared preserve item.",
}


@app.post("/suggest_variants")
def suggest_variants(req: aux_v2.SuggestRequest, execution_id: str | None = Depends(gpu_work)) -> dict:
    images = [(_prep(i.b64), i.view or "view") for i in req.images]
    system = (PROMPTS / "system_prompt_suggest_variants.txt").read_text().format(
        kind=req.kind or "object", request=req.request, preserve=req.preserve or "(identity of the source)",
        observations="; ".join(req.observations) or "(none)", count=req.count, intent=req.intent,
        intent_rules=INTENT_RULES[req.intent], image_note="attached below" if images else "(none)")
    messages = [_sys(system), {"role": "user", "content": _images_block(images)
                               + [{"type": "text", "text": f"Propose {req.count} variant rows."}]}]
    t0 = time.monotonic()
    result, raw = _generate_parsed(messages, lambda r: aux_v2.parse_suggestions(r, req.count),
                                   min(4096, 200 + 90 * req.count))
    return {**result, "meta": {**_meta(t0, execution_id), "raw": raw[:4000]}}


class CutoutRequest(BaseModel):
    image_b64: str


@app.post("/cutout")
def cutout(req: CutoutRequest, execution_id: str | None = Depends(gpu_work)) -> dict:
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
    return {"mask_b64": base64.b64encode(out.getvalue()).decode(),
            "meta": {"model": "BiRefNet", "execution_id": execution_id, "birefnet_loads": birefnet.loads}}


@app.get("/health")
def health() -> dict:
    return {"ok": True, "models_present": {"vlm": (VLM_DIR / "config.json").is_file(),
                                           "birefnet": (BIREFNET_DIR / "config.json").is_file()},
            "loaded": {"vlm": vlm.loaded, "birefnet": birefnet.loaded}, "loads": {"vlm": vlm.loads,
                                                                                  "birefnet": birefnet.loads},
            "cuda": torch.cuda.is_available(), "gpu": gpu_info(), "lease": lease.info()}


class LeaseRequest(BaseModel):
    epoch: int = Field(ge=1)


@app.post("/lease")
def grant(req: LeaseRequest) -> dict:
    try:
        return lease.grant(req.epoch)
    except StaleLease as e:
        raise HTTPException(409, f"stale_lease: {e}") from e


class UnloadRequest(BaseModel):
    owner_token: str = Field(min_length=1, max_length=100)
    epoch: int = Field(ge=0)


@app.post("/unload")
def unload(req: UnloadRequest) -> dict:
    """Acknowledges release only after admission stopped, no request is active and both models are gone."""
    try:
        drained = lease.drain(req.epoch, timeout=300.0)
    except StaleLease as e:
        raise HTTPException(409, f"stale_lease: {e}") from e
    if not drained or not (vlm.unload() and birefnet.unload()):
        raise HTTPException(409, "models still in use")
    return {"loaded": vlm.loaded or birefnet.loaded, "owner_token": req.owner_token, **lease.info()}
