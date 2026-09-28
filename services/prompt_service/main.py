"""prompt-service: Qwen3-VL-8B prompt enhancement + image QA."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

import torch
import yaml
from fastapi import FastAPI, HTTPException
from lazy_model import LazyModel
from pydantic import BaseModel, Field, ValidationError

MODEL_DIR = Path(os.environ.get("QWEN3_VL_DIR", "/models/checkpoints/qwen3-vl-8b-instruct"))
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", "/output")).resolve()
IDLE_UNLOAD_S = float(os.environ.get("IDLE_UNLOAD_S", "300"))

ASSET_TYPE_HINTS = {
    "small_prop": "small hand-sized or knee-high prop; compact chunky proportions",
    "medium_prop": "medium prop such as furniture or a crate stack; sturdy readable proportions",
    "large_prop": "large prop such as a cart or statue; bold simple masses",
    "rock": "a single rock or boulder; clear faceted planes, no moss carpet or ground",
    "tree_trunk": "a single tree trunk or stump, no leaves canopy, no roots spreading on ground",
    "plant": "a single potted or standalone plant, compact silhouette",
    "weapon": "a single weapon lying in no particular scene, shown fully",
}
DEFAULT_ASSET_HINT = "a generic standalone game prop"


def _load() -> tuple[object, object]:
    from transformers import AutoModelForImageTextToText, AutoProcessor

    model = AutoModelForImageTextToText.from_pretrained(
        MODEL_DIR, dtype=torch.bfloat16, device_map="cuda:0", local_files_only=True
    )
    processor = AutoProcessor.from_pretrained(MODEL_DIR, local_files_only=True)
    return model, processor


vlm = LazyModel(_load, IDLE_UNLOAD_S)
app = FastAPI(title="line-a prompt-service")


def _read_prompt(name: str) -> str:
    return (CONFIG_DIR / "prompts" / name).read_text().strip()


def _model_info() -> dict:
    spec = yaml.safe_load((CONFIG_DIR / "models.yaml").read_text())["models"]["qwen3_vl_8b_instruct"]
    return {"repo": spec["repo"], "revision": spec["revision"]}


def _generate(messages: list[dict], max_new_tokens: int) -> str:
    model, processor = vlm.get()
    inputs = processor.apply_chat_template(
        messages, tokenize=True, add_generation_prompt=True, return_dict=True, return_tensors="pt"
    ).to(model.device)
    with torch.inference_mode():
        out = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
    return processor.batch_decode(out[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)[0]


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in model output")
    return json.loads(match.group(0))


def _generate_json(messages: list[dict], schema: type[BaseModel], max_new_tokens: int) -> tuple[BaseModel, str]:
    """Greedy decode, validate; on failure retry once with the error fed back."""
    raw = _generate(messages, max_new_tokens)
    try:
        return schema.model_validate(_extract_json(raw)), raw
    except (ValueError, ValidationError) as e:
        retry = messages + [
            {"role": "assistant", "content": [{"type": "text", "text": raw}]},
            {"role": "user", "content": [{"type": "text", "text": f"Invalid output ({e}). Reply with the JSON object only."}]},
        ]
        raw = _generate(retry, max_new_tokens)
        try:
            return schema.model_validate(_extract_json(raw)), raw
        except (ValueError, ValidationError) as e2:
            raise HTTPException(502, f"model returned invalid JSON twice: {e2}; raw={raw[:500]!r}") from e2


class EnhanceRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    asset_type: str | None = None
    target_triangles: int | None = Field(default=None, ge=1)


class EnhanceResult(BaseModel):
    enhanced_prompt: str
    short_title: str = ""
    asset_tags: list[str] = []
    camera_hint: str = ""
    qa_focus: list[str] = []


@app.post("/enhance")
def enhance(req: EnhanceRequest) -> dict:
    template = _read_prompt("model_sheet_template.txt")
    system_tpl = _read_prompt("system_prompt_enhancer.txt")
    tri_hint = (
        f"target budget about {req.target_triangles} triangles: favor simple, bold, low-detail forms"
        if req.target_triangles
        else "no triangle budget given"
    )
    system = system_tpl.format(
        template=template,
        asset_type_hint=ASSET_TYPE_HINTS.get(req.asset_type or "", DEFAULT_ASSET_HINT),
        triangle_hint=tri_hint,
    )
    messages = [
        {"role": "system", "content": [{"type": "text", "text": system}]},
        {"role": "user", "content": [{"type": "text", "text": req.prompt}]},
    ]
    t0 = time.monotonic()
    result, raw = _generate_json(messages, EnhanceResult, max_new_tokens=512)
    assert isinstance(result, EnhanceResult)
    # The model-sheet block is mandatory regardless of what the model wrote.
    if template.lower() not in result.enhanced_prompt.lower():
        result.enhanced_prompt = f"{result.enhanced_prompt.rstrip(' .,')}, {template}"
    return {
        **result.model_dump(),
        "meta": {
            "model": _model_info(),
            "template_sha256": hashlib.sha256((system_tpl + template).encode()).hexdigest()[:16],
            "seconds": round(time.monotonic() - t0, 2),
            "raw": raw,
        },
    }


class QARequest(BaseModel):
    image_path: str  # relative to OUTPUT_ROOT, e.g. "<job>/candidates/00.png"
    prompt: str | None = None


class QAResult(BaseModel):
    checks: dict[str, bool]
    confidence: dict[str, float] = {}
    reasons: list[str] = []
    summary: str = ""


@app.post("/qa")
def qa(req: QARequest) -> dict:
    image = (OUTPUT_ROOT / req.image_path).resolve()
    if not image.is_relative_to(OUTPUT_ROOT) or not image.is_file():
        raise HTTPException(400, f"bad image_path: {req.image_path}")
    rules = yaml.safe_load((CONFIG_DIR / "qa" / "rules.yaml").read_text())["vlm_checks"]
    checks = "\n".join(f"- {cid}: {r['q']}" for cid, r in rules.items())
    system = _read_prompt("system_prompt_qa.txt").format(checks=checks)
    user_text = "Inspect this image." + (f" It was generated for: {req.prompt}" if req.prompt else "")
    messages = [
        {"role": "system", "content": [{"type": "text", "text": system}]},
        {"role": "user", "content": [{"type": "image", "image": str(image)}, {"type": "text", "text": user_text}]},
    ]
    t0 = time.monotonic()
    result, raw = _generate_json(messages, QAResult, max_new_tokens=768)
    assert isinstance(result, QAResult)
    missing = [cid for cid in rules if cid not in result.checks]
    return {
        **result.model_dump(),
        "missing_checks": missing,
        "meta": {"model": _model_info(), "seconds": round(time.monotonic() - t0, 2)},
    }


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "model_present": (MODEL_DIR / "config.json").is_file(),
        "loaded": vlm.loaded,
        "cuda": torch.cuda.is_available(),
    }


@app.post("/unload")
def unload() -> dict:
    vlm.unload()
    return {"loaded": False}
