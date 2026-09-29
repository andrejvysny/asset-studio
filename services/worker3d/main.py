"""worker3d: TRELLIS.2 (image -> raw mesh + attribute volume) and GLB export. Stateless: bytes in, bytes out.

The Studio stores every result; this service keeps nothing but lazily loaded weights, released on /unload.
"""
from __future__ import annotations

import base64
import io
import json
import os
import threading
import time
from typing import Literal

import raw as rawio
import torch
from export import to_glb
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from lazy_model import LazyModel, gpu_info
from PIL import Image
from pipeline_config import MODELS, TRELLIS_DIR, write_local_pipeline_json
from pydantic import BaseModel, Field
from rasterize import available

IDLE_UNLOAD_S = float(os.environ.get("IDLE_UNLOAD_S", "300"))
TRELLIS_REF = "75fbf0183001ed9876c8dbb35de6b68552ee08bd"
MAX_RAW_BYTES = 1 << 30
LIMITATIONS = [f"export always fills holes with perimeter < {0.03} (upstream constant)"]
EXPORTER_LICENCE = {"clean": "MIT (TRELLIS.2 o-voxel port + AssetStudio UV rasteriser)",
                    "research": "NVIDIA Source Code License (nvdiffrast v0.4.0): research/evaluation only"}


def missing_models() -> list[str]:
    checks = {"trellis2": (TRELLIS_DIR / "pipeline.json").is_file(),
              "trellis_image_large": any((MODELS / "trellis-image-large" / "ckpts").glob("*.safetensors")),
              "dinov3_vitl16": (MODELS / "dinov3-vitl16" / "model.safetensors").is_file()}
    return [k for k, ok in checks.items() if not ok]


def _load() -> object:
    from trellis2.pipelines import Trellis2ImageTo3DPipeline

    if missing := missing_models():
        raise RuntimeError(f"missing model weights: {', '.join(missing)}")
    pipe = Trellis2ImageTo3DPipeline.from_pretrained(str(write_local_pipeline_json()))
    pipe.cuda()
    return pipe


trellis = LazyModel(_load, IDLE_UNLOAD_S)
gpu = threading.Lock()  # one GPU job at a time
app = FastAPI(title="assetstudio worker3d")


class GenerateRequest(BaseModel):
    image_b64: str = Field(max_length=64 * 2**20)  # RGBA cut-out PNG (alpha = foreground)
    seed: int = Field(ge=0, lt=2**31)
    pipeline_type: Literal["512", "1024", "1024_cascade", "1536_cascade"] = "1024_cascade"


def _meta_headers(meta: dict) -> dict[str, str]:
    return {"x-worker-meta": json.dumps(meta, separators=(",", ":"))}


@app.post("/generate")
def generate(req: GenerateRequest) -> Response:
    try:
        image = Image.open(io.BytesIO(base64.b64decode(req.image_b64, validate=True)))
        image.load()
    except (ValueError, OSError) as e:
        raise HTTPException(422, f"not a decodable image: {e}") from e
    if image.mode != "RGBA":
        raise HTTPException(422, "expected an RGBA cut-out (alpha = foreground)")
    t0 = time.monotonic()
    with gpu, trellis.use() as pipe:
        torch.cuda.reset_peak_memory_stats()
        torch.manual_seed(req.seed)
        mesh = pipe.run(image, seed=req.seed, pipeline_type=req.pipeline_type)[0]  # type: ignore[attr-defined]
        t1 = time.monotonic()
        data = rawio.dump(mesh)
        faces = int(mesh.faces.shape[0])
        del mesh
        torch.cuda.empty_cache()
    return Response(data, media_type="application/octet-stream", headers=_meta_headers({
        "engine": "TRELLIS.2", "trellis_ref": TRELLIS_REF, "pipeline_type": req.pipeline_type, "seed": req.seed,
        "raw_format": rawio.FORMAT, "raw_faces": faces, "timings_s": {"sample": round(t1 - t0, 2)},
        "peak_vram_mb": torch.cuda.max_memory_allocated() // 2**20}))


@app.post("/export")
async def export(request: Request, exporter: Literal["clean", "research"] = "clean",
                 decimation_target: int = Query(200_000, ge=1_000, le=2_000_000),
                 texture_size: int = Query(2048, ge=256, le=8192), remesh: bool = False) -> Response:
    if not available()[exporter]:
        raise HTTPException(422, f"exporter {exporter!r} is not installed in this worker image")
    body = await request.body()
    if not body or len(body) > MAX_RAW_BYTES:
        raise HTTPException(413, "raw payload missing or too large")
    glb, meta = await run_in_threadpool(_export, body, exporter, decimation_target, texture_size, remesh)
    meta.update(licence=EXPORTER_LICENCE[exporter], limitations=LIMITATIONS)
    return Response(glb, media_type="model/gltf-binary", headers=_meta_headers(meta))


def _export(body: bytes, exporter: str, decimation_target: int, texture_size: int, remesh: bool) -> tuple[bytes, dict]:
    with gpu:
        try:
            raw = rawio.load(body)
        except (ValueError, KeyError, OSError) as e:
            raise HTTPException(422, f"invalid raw intermediate: {e}") from e
        try:
            return to_glb(raw, exporter, decimation_target, texture_size, remesh)
        finally:
            del raw
            torch.cuda.empty_cache()


@app.get("/health")
def health() -> dict:
    missing = missing_models()
    return {"ok": not missing and torch.cuda.is_available(), "missing_models": missing,
            "models_present": {k: k not in missing for k in ("trellis2", "trellis_image_large", "dinov3_vitl16")},
            "loaded": {"trellis2": trellis.loaded}, "loads": {"trellis2": trellis.loads}, "exporters": available(),
            "trellis_ref": TRELLIS_REF, "cuda": torch.cuda.is_available(), "gpu": gpu_info()}


class UnloadRequest(BaseModel):
    owner_token: str = Field(min_length=1, max_length=100)


@app.post("/unload")
def unload(req: UnloadRequest) -> dict:
    """Acknowledges release only once the weights are really gone (waits for the in-flight job)."""
    if not trellis.unload():
        raise HTTPException(409, "model still in use")
    return {"loaded": trellis.loaded, "owner_token": req.owner_token}
