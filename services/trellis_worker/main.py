"""trellis-worker: BiRefNet cut-out + TRELLIS.2 generation/export. Writes into /output/<job>/."""
from __future__ import annotations

import os
import re
import threading
import time
import traceback
from pathlib import Path

import cutout as cut
import generate as gen
import torch
from fastapi import FastAPI, HTTPException
from lazy_model import LazyModel
from PIL import Image
from pipeline_config import TRELLIS_DIR, write_local_pipeline_json
from pydantic import BaseModel, Field

OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", "/output")).resolve()
IDLE_UNLOAD_S = float(os.environ.get("IDLE_UNLOAD_S", "300"))
JOB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


def _load_trellis() -> object:
    from trellis2.pipelines import Trellis2ImageTo3DPipeline

    pipe = Trellis2ImageTo3DPipeline.from_pretrained(str(write_local_pipeline_json()))
    pipe.cuda()
    return pipe


trellis = LazyModel(_load_trellis, IDLE_UNLOAD_S)
birefnet = LazyModel(cut.load_birefnet, IDLE_UNLOAD_S)
_gpu_lock = threading.Lock()  # one GPU job at a time on this worker
app = FastAPI(title="line-a trellis-worker")


def job_path(job_id: str, rel: str) -> Path:
    if not JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(400, f"invalid job id {job_id!r}")
    root = (OUTPUT_ROOT / job_id).resolve()
    p = (root / rel).resolve()
    if not root.is_dir() or not p.is_relative_to(root):
        raise HTTPException(400, f"bad path {job_id}/{rel}")
    return p


class CutoutRequest(BaseModel):
    job_id: str
    src: str                      # e.g. candidates/00.png
    dst_rgba: str                 # e.g. qa/cutouts/00.png
    dst_mask: str | None = None
    alpha_threshold: int = 128
    border_px: int = 4


@app.post("/cutout")
def cutout(req: CutoutRequest) -> dict:
    src = job_path(req.job_id, req.src)
    if not src.is_file():
        raise HTTPException(404, f"missing {req.src}")
    with _gpu_lock:
        image = Image.open(src)
        mask = cut.predict_mask(birefnet.get(), image)
    dst_mask = job_path(req.job_id, req.dst_mask) if req.dst_mask else None
    cut.write_cutout(image, mask, job_path(req.job_id, req.dst_rgba), dst_mask)
    return {"stats": cut.mask_stats(mask, req.alpha_threshold, req.border_px)}


class GenerateRequest(BaseModel):
    job_id: str
    input: str = "cutout/cutout.png"
    seed: int = 42
    pipeline_type: str = "1024_cascade"
    decimation_target: int = Field(default=200000, ge=500)
    texture_size: int = Field(default=2048, ge=512, le=4096)
    min_component_ratio: float = Field(default=0.01, ge=0, le=0.5)


@app.post("/generate")
def generate(req: GenerateRequest) -> dict:
    src = job_path(req.job_id, req.input)
    if not src.is_file():
        raise HTTPException(404, f"missing {req.input}")
    raw_dir = job_path(req.job_id, "model/raw")
    proc_dir = job_path(req.job_id, "model/processed")
    log_dir = job_path(req.job_id, "model/logs")
    timings: dict[str, float] = {}
    stage = "load"
    try:
        with _gpu_lock:
            birefnet.unload()
            t = time.monotonic()
            pipe = trellis.get()
            timings["load_s"] = round(time.monotonic() - t, 1)

            stage, t = "trellis", time.monotonic()
            mesh = gen.run_trellis(pipe, Image.open(src).convert("RGBA"), req.seed, req.pipeline_type)
            timings["trellis_s"] = round(time.monotonic() - t, 1)
            raw_dir.mkdir(parents=True, exist_ok=True)
            gen.write_json(raw_dir / "raw_mesh.json", {"vertices": int(mesh.vertices.shape[0]), "faces": int(mesh.faces.shape[0])})

            stage, t = "preview", time.monotonic()
            preview_error = None
            try:
                gen.render_preview(mesh, proc_dir / "preview.png")
            except Exception as e:  # preview is optional
                preview_error = f"{type(e).__name__}: {e}"
            timings["preview_s"] = round(time.monotonic() - t, 1)

            stage, t = "export", time.monotonic()
            glb = gen.export_glb(mesh, req.decimation_target, req.texture_size)
            del mesh
            torch.cuda.empty_cache()
            timings["export_s"] = round(time.monotonic() - t, 1)

        stage = "postprocess"
        glb, removed = gen.drop_floaters(glb, req.min_component_ratio)
        glb_path = proc_dir / "model.glb"
        proc_dir.mkdir(parents=True, exist_ok=True)
        glb.export(glb_path)
        textures = gen.save_textures(glb, proc_dir / "textures")
        info = gen.mesh_info(glb, glb_path, removed)
        info["decimation_target"] = req.decimation_target
        gen.write_json(proc_dir / "mesh_info.json", info)
    except HTTPException:
        raise
    except Exception as e:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "error.log").write_text(traceback.format_exc())
        raise HTTPException(500, {"stage": stage, "error": f"{type(e).__name__}: {e}"}) from e
    return {
        "glb": "model/processed/model.glb",
        "mesh_info": info,
        "textures": textures,
        "preview": None if preview_error else "model/processed/preview.png",
        "preview_error": preview_error,
        "timings": timings,
        "params": req.model_dump(),
    }


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "model_present": (TRELLIS_DIR / "pipeline.json").is_file() and (cut.BIREFNET_DIR / "config.json").is_file(),
        "trellis_loaded": trellis.loaded,
        "birefnet_loaded": birefnet.loaded,
        "cuda": torch.cuda.is_available(),
    }


@app.post("/unload")
def unload() -> dict:
    with _gpu_lock:
        trellis.unload()
        birefnet.unload()
    return {"loaded": False}
