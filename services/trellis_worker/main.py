"""trellis-worker: BiRefNet cut-out + TRELLIS.2 generation/export. 3D writes go to /output/<job>/model/attempts/att-NN/."""
from __future__ import annotations

import os
import re
import threading
import time
import traceback
from pathlib import Path

import cutout as cut
import generate as gen
import meshcheck
import torch
from fastapi import FastAPI, HTTPException
from lazy_model import LazyModel, gpu_info
from PIL import Image
from pipeline_config import MODELS, TRELLIS_DIR, write_local_pipeline_json
from pydantic import BaseModel, Field

OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", "/output")).resolve()
IDLE_UNLOAD_S = float(os.environ.get("IDLE_UNLOAD_S", "300"))
JOB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
ATTEMPT_DIR_RE = re.compile(r"^model/attempts/att-\d{2,4}$")


def missing_models() -> list[str]:
    checks = {
        "trellis2": (TRELLIS_DIR / "pipeline.json").is_file(),
        "trellis_image_large": any((MODELS / "trellis-image-large" / "ckpts").glob("*.safetensors")),
        "dinov3_vitl16": any((MODELS / "dinov3-vitl16").glob("*.safetensors")),
        "birefnet": (cut.BIREFNET_DIR / "config.json").is_file(),
    }
    return [k for k, ok in checks.items() if not ok]


def _load_trellis() -> object:
    from trellis2.pipelines import Trellis2ImageTo3DPipeline

    missing = missing_models()
    if missing:  # fail fast with an actionable message instead of a deep transformers OSError
        raise RuntimeError(f"missing model weights: {', '.join(missing)} (run scripts/download-models.sh; "
                           "dinov3 is gated on Hugging Face)")

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
    if not root.is_dir() or root.parent != OUTPUT_ROOT or not p.is_relative_to(root):
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


class Cleanup(BaseModel):
    remesh: bool = False          # off by default: can change intentional shapes
    drop_floaters: bool = False   # off by default: small parts can be legitimate
    min_component_ratio: float = Field(default=0.01, ge=0, le=0.5)


class ExportParams(BaseModel):
    job_id: str
    attempt_dir: str              # model/attempts/att-NN
    decimation_target: int = Field(default=200000, ge=500)
    texture_size: int = Field(default=2048, ge=512, le=4096)
    cleanup: Cleanup = Cleanup()


class GenerateRequest(ExportParams):
    seed: int = 42
    pipeline_type: str = "1024_cascade"


class ReexportRequest(ExportParams):
    from_attempt_dir: str


def _attempt_path(job_id: str, attempt_dir: str) -> Path:
    if not ATTEMPT_DIR_RE.fullmatch(attempt_dir):
        raise HTTPException(400, f"bad attempt_dir {attempt_dir!r}")
    return job_path(job_id, attempt_dir)


def _fail(adir: Path, stage: str, e: Exception) -> HTTPException:
    (adir / "logs").mkdir(parents=True, exist_ok=True)
    (adir / "logs" / f"error_{stage}.log").write_text(traceback.format_exc())
    return HTTPException(500, {"stage": stage, "error": f"{type(e).__name__}: {e}"})


def _export(mesh: object, adir: Path, req: ExportParams, timings: dict) -> dict:
    """export -> optional cleanup -> write -> validate. Raises HTTPException tagged with the failing stage."""
    stage, t = "export", time.monotonic()
    try:
        glb = gen.export_glb(mesh, req.decimation_target, req.texture_size, req.cleanup.remesh)
        torch.cuda.empty_cache()
        timings["export_s"] = round(time.monotonic() - t, 1)
        stage = "postprocess"
        removed = 0
        if req.cleanup.drop_floaters:
            glb, removed = meshcheck.drop_floaters(glb, req.cleanup.min_component_ratio)
        proc = adir / "processed"
        proc.mkdir(parents=True, exist_ok=True)
        glb_path = proc / "model.glb"
        glb.export(glb_path)
        textures = meshcheck.save_textures(glb, proc / "textures")
        info = meshcheck.mesh_info(glb, glb_path, removed)
        gen.write_json(proc / "mesh_info.json", info)
        stage = "validation"
        validation = meshcheck.validate_glb(glb_path)
        gen.write_json(proc / "validation.json", validation)
        if not validation["ok"]:
            failed = [c["id"] for c in validation["checks"] if not c["ok"]]
            raise ValueError(f"exported GLB failed validation: {failed}")
    except HTTPException:
        raise
    except Exception as e:
        raise _fail(adir, stage, e) from e
    return {"mesh_info": info, "validation": validation, "textures": textures}


@app.post("/generate")
def generate(req: GenerateRequest) -> dict:
    adir = _attempt_path(req.job_id, req.attempt_dir)
    src = adir / "cutout.png"
    if not src.is_file():
        raise HTTPException(404, f"missing {req.attempt_dir}/cutout.png")
    timings: dict[str, float] = {}
    with _gpu_lock:
        stage = "load"
        try:
            birefnet.unload()
            t = time.monotonic()
            pipe = trellis.get()
            timings["load_s"] = round(time.monotonic() - t, 1)
            stage, t = "trellis", time.monotonic()
            mesh = gen.run_trellis(pipe, Image.open(src).convert("RGBA"), req.seed, req.pipeline_type)
            timings["trellis_s"] = round(time.monotonic() - t, 1)
            stage = "raw"
            gen.save_raw(mesh, adir / "raw" / "raw.pt")
        except Exception as e:
            raise _fail(adir, stage, e) from e
        preview_error = None
        try:
            gen.render_preview(mesh, adir / "processed" / "preview.png")
        except Exception as e:  # optional, never blocks export
            preview_error = f"{type(e).__name__}: {e}"
        result = _export(mesh, adir, req, timings)
        del mesh
    return _response(result, req, timings, preview_error)


@app.post("/reexport")
def reexport(req: ReexportRequest) -> dict:
    adir = _attempt_path(req.job_id, req.attempt_dir)
    raw = _attempt_path(req.job_id, req.from_attempt_dir) / "raw" / "raw.pt"
    if not raw.is_file():
        raise HTTPException(404, f"missing {req.from_attempt_dir}/raw/raw.pt")
    timings: dict[str, float] = {}
    with _gpu_lock:
        try:
            mesh = gen.load_raw(raw)
        except Exception as e:
            raise _fail(adir, "raw", e) from e
        result = _export(mesh, adir, req, timings)
        del mesh
    return _response(result, req, timings, "re-export: no preview render", raw_from=req.from_attempt_dir)


def _response(result: dict, req: ExportParams, timings: dict, preview_error: str | None, raw_from: str | None = None) -> dict:
    has_preview = preview_error is None
    return {
        **result,
        "outputs": {"glb": "processed/model.glb", "mesh_info": "processed/mesh_info.json",
                    "validation": "processed/validation.json", "raw": None if raw_from else "raw/raw.pt",
                    "preview": "processed/preview.png" if has_preview else None},
        "preview_source": "trellis_raw_mesh_pre_export" if has_preview else None,
        "preview_error": preview_error,
        "raw_from": raw_from,
        "timings": timings,
        "params": req.model_dump(),
        "known_limitations": gen.KNOWN_LIMITATIONS,
    }


@app.get("/health")
def health() -> dict:
    missing = missing_models()
    models = {k: k not in missing for k in ("trellis2", "trellis_image_large", "dinov3_vitl16", "birefnet")}
    return {
        "ok": all(models.values()) and torch.cuda.is_available(),
        "models": models,
        "model_present": all(models.values()),
        "trellis_loaded": trellis.loaded,
        "birefnet_loaded": birefnet.loaded,
        "cuda": torch.cuda.is_available(),
        "gpu": gpu_info(),
    }


@app.post("/unload")
def unload() -> dict:
    with _gpu_lock:
        trellis.unload()
        birefnet.unload()
    return {"loaded": False}
