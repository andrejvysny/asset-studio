"""worker3d: TRELLIS.2 (image -> raw mesh + attribute volume) and GLB export as recoverable executions.

Owns no product state. Each execution (id chosen by the Studio) is spooled on a persistent volume until the Studio
acknowledges ingestion, so a lost HTTP response or a Studio restart never forces a second TRELLIS.2 run. Work that
had not finished when THIS process died is reported `lost` (never silently re-run).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import queue
import shutil
import time
from pathlib import Path
from typing import Any, Literal

import raw as rawio
import spool
import torch
from export import to_glb
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from lazy_model import LazyModel, gpu_info
from lease import Lease, StaleLease
from PIL import Image
from pipeline_config import MODELS, TRELLIS_DIR, write_local_pipeline_json
from pydantic import BaseModel, Field
from rasterize import available

IDLE_UNLOAD_S = float(os.environ.get("IDLE_UNLOAD_S", "300"))
SPOOL = spool.SPOOL
TRELLIS_REF = "75fbf0183001ed9876c8dbb35de6b68552ee08bd"
MAX_IMAGE_BYTES = 64 * 2**20
LIMITATIONS = [f"export fills holes with perimeter < {0.03} (upstream constant) unless fill_holes=disabled"]
EXPORTER_LICENCE = {"clean": "MIT (TRELLIS.2 o-voxel port + AssetStudio UV rasteriser)",
                    "research": "NVIDIA Source Code License (nvdiffrast v0.4.0): research/evaluation only"}
TERMINAL = spool.TERMINAL


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
lease = Lease()
jobs: queue.Queue[str] = queue.Queue()
cancel_flags: set[str] = set()
app = FastAPI(title="assetstudio worker3d")


# --- spool -------------------------------------------------------------------------------------------------------
def _dir(eid: str) -> Path:
    if not spool.EXEC_ID.fullmatch(eid):
        raise HTTPException(400, "invalid execution id")
    return SPOOL / eid


def _state(eid: str) -> dict[str, Any] | None:
    _dir(eid)
    return spool.state(eid)


# --- execution ---------------------------------------------------------------------------------------------------
def _cancelled(eid: str) -> bool:
    return eid in cancel_flags


def _run_generate(eid: str, params: dict[str, Any], body: bytes) -> tuple[bytes, dict[str, Any]]:
    image = Image.open(io.BytesIO(body))
    image.load()
    t0 = time.monotonic()
    with trellis.use() as pipe:
        torch.cuda.reset_peak_memory_stats()
        torch.manual_seed(params["seed"])
        mesh = pipe.run(image, seed=params["seed"], pipeline_type=params["pipeline_type"])[0]  # type: ignore
        t1 = time.monotonic()
        data = rawio.dump(mesh)
        faces = int(mesh.faces.shape[0])
        del mesh
        torch.cuda.empty_cache()
    return data, {"engine": "TRELLIS.2", "trellis_ref": TRELLIS_REF, "pipeline_type": params["pipeline_type"],
                  "seed": params["seed"], "raw_format": rawio.FORMAT, "raw_faces": faces,
                  "timings_s": {"sample": round(t1 - t0, 2)},
                  "peak_vram_mb": torch.cuda.max_memory_allocated() // 2**20, "trellis_loads": trellis.loads}


def _run_export(eid: str, params: dict[str, Any], body: bytes) -> tuple[bytes, dict[str, Any]]:
    raw = rawio.validate(body)  # CPU schema/resource checks before any tensor reaches the GPU
    if _cancelled(eid):
        raise InterruptedError()
    torch.cuda.reset_peak_memory_stats()
    cuda_raw = rawio.to_cuda(raw)
    try:
        glb, meta = to_glb(cuda_raw, params["exporter"], params["decimation_target"], params["texture_size"],
                           params["remesh"], params["small_components"], params["fill_holes"])
    finally:
        del cuda_raw
        torch.cuda.empty_cache()
    meta.update(licence=EXPORTER_LICENCE[params["exporter"]], limitations=LIMITATIONS,
                peak_vram_mb=torch.cuda.max_memory_allocated() // 2**20)
    return glb, meta


def _guarded(fn: spool.Runner) -> spool.Runner:
    """Map torch/raw-specific failures to typed spool failures."""
    def run(eid: str, params: dict[str, Any], body: bytes) -> tuple[bytes, dict[str, Any]]:
        try:
            return fn(eid, params, body)
        except rawio.RawInvalid as e:
            raise spool.ExecutionFailed("input_invalid", f"invalid raw intermediate: {e}") from e
        except torch.cuda.OutOfMemoryError as e:
            raise spool.ExecutionFailed("oom", f"out of GPU memory: {str(e)[:200]}") from e
    return run


executor = spool.Executor(jobs, {"generate": _guarded(_run_generate), "export": _guarded(_run_export)},
                          cancel_flags, lease)


@app.on_event("startup")
def _startup() -> None:
    spool.recover()
    executor.start()


def _epoch(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


@app.post("/executions/{eid}", status_code=202)
async def submit(eid: str, request: Request, op: Literal["generate", "export"],
                 x_lease_epoch: str | None = Header(default=None),
                 x_exec_params: str = Header(max_length=4000)) -> dict[str, Any]:
    """Idempotent by id: the same id + request returns the current state; a different request conflicts."""
    body = await request.body()
    try:
        params = _params(op, json.loads(x_exec_params))
    except (ValueError, TypeError, KeyError) as e:
        raise HTTPException(422, f"invalid parameters: {e}") from e
    req_sha = hashlib.sha256(json.dumps({"op": op, "params": params}, sort_keys=True).encode() + body).hexdigest()
    d = _dir(eid)
    if (st := _state(eid)) is not None:
        if st.get("request_sha256") != req_sha:
            raise HTTPException(409, "execution id reused with a different request")
        return st
    _validate_input(op, params, body)
    try:
        lease.enter(_epoch(x_lease_epoch))
    except StaleLease as e:
        raise HTTPException(409, f"stale_lease: {e}") from e
    try:
        d.mkdir(parents=True)
        spool.write(d / "input.bin", body)
        spool.write(d / "request.json", json.dumps({"op": op, "params": params}).encode())
        spool.set_state(eid, state="queued", request_sha256=req_sha, session_id=lease.session_id,
                   epoch=lease.epoch, submitted_at=time.time())
    except BaseException:
        lease.leave()
        shutil.rmtree(d, ignore_errors=True)
        raise
    jobs.put(eid)
    return _state(eid) or {}


EXPORT_KEYS = {"exporter", "decimation_target", "texture_size", "remesh", "small_components", "fill_holes"}


def _params(op: str, p: dict[str, Any]) -> dict[str, Any]:
    if op == "generate":
        if unknown := set(p) - {"seed", "pipeline_type"}:
            raise ValueError(f"unknown generate params: {sorted(unknown)}")
        pt = p["pipeline_type"]
        if pt not in ("512", "1024", "1024_cascade", "1536_cascade") or not 0 <= int(p["seed"]) < 2**31:
            raise ValueError("pipeline_type/seed out of range")
        return {"seed": int(p["seed"]), "pipeline_type": pt}
    if unknown := set(p) - EXPORT_KEYS:
        raise ValueError(f"unknown export params: {sorted(unknown)}")
    exporter = p["exporter"]
    if exporter not in ("clean", "research"):
        raise ValueError("unknown exporter")
    target, size = int(p["decimation_target"]), int(p["texture_size"])
    if not (1_000 <= target <= 2_000_000 and 256 <= size <= 8192):
        raise ValueError("decimation_target/texture_size out of range")
    small, holes = p.get("small_components", "remove"), p.get("fill_holes", "upstream")
    if small not in ("remove", "preserve") or holes not in ("upstream", "disabled"):
        raise ValueError("small_components/fill_holes out of range")
    return {"exporter": exporter, "decimation_target": target, "texture_size": size, "remesh": bool(p["remesh"]),
            "small_components": small, "fill_holes": holes}


def _validate_input(op: str, params: dict[str, Any], body: bytes) -> None:
    """Reject bad inputs at admission (typed 4xx), before queueing GPU work."""
    if op == "generate":
        if not body or len(body) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "image missing or too large")
        try:
            with Image.open(io.BytesIO(body)) as im:
                im.load()
                if im.mode != "RGBA":
                    raise HTTPException(422, "expected an RGBA cut-out (alpha = foreground)")
        except (OSError, ValueError) as e:
            raise HTTPException(422, f"not a decodable image: {e}") from e
        return
    if not available()[params["exporter"]]:
        raise HTTPException(422, f"exporter {params['exporter']!r} is not installed in this worker image")
    try:
        rawio.validate(body)
    except rawio.RawInvalid as e:
        raise HTTPException(422, f"invalid raw intermediate: {e}") from e


@app.get("/executions/{eid}")
def execution(eid: str) -> dict[str, Any]:
    st = _state(eid)
    if st is None:
        raise HTTPException(404, "unknown execution")
    return {**st, "worker_session": lease.session_id}


@app.get("/executions/{eid}/result")
def execution_result(eid: str) -> Response:
    st = _state(eid)
    if st is None or st.get("state") != "succeeded":
        raise HTTPException(409, "no result")
    d = _dir(eid)
    return Response((d / "result.bin").read_bytes(), media_type="application/octet-stream",
                    headers={"x-worker-meta": (d / "meta.json").read_text()})


@app.post("/executions/{eid}/cancel")
def execution_cancel(eid: str) -> dict[str, Any]:
    """Best effort: honoured before start and at phase boundaries; a running sample finishes (then is discarded)."""
    st = _state(eid)
    if st is None:
        raise HTTPException(404, "unknown execution")
    if st.get("state") not in TERMINAL:
        cancel_flags.add(eid)
    return {**st, "cancel_requested": st.get("state") not in TERMINAL}


@app.delete("/executions/{eid}")
def execution_ack(eid: str) -> dict[str, Any]:
    """The Studio verified and stored the result: the spool entry can go."""
    st = _state(eid)
    if st is not None and st.get("state") not in TERMINAL:
        raise HTTPException(409, "execution still active")
    shutil.rmtree(_dir(eid), ignore_errors=True)
    return {"acknowledged": True}


@app.get("/health")
def health() -> dict:
    missing = missing_models()
    spooled = sum(1 for _ in SPOOL.iterdir()) if SPOOL.is_dir() else 0
    ex = executor.health()
    return {"ok": not missing and torch.cuda.is_available() and ex["alive"], "executor": ex, "missing_models": missing,
            "models_present": {k: k not in missing for k in ("trellis2", "trellis_image_large", "dinov3_vitl16")},
            "loaded": {"trellis2": trellis.loaded}, "loads": {"trellis2": trellis.loads}, "exporters": available(),
            "export_features": ["geometry_policy.v1"],
            "trellis_ref": TRELLIS_REF, "cuda": torch.cuda.is_available(), "gpu": gpu_info(),
            "lease": lease.info(), "queued": jobs.qsize(), "spooled": spooled}


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
    """Acknowledges release only when NOTHING is queued or running (sampling, export, transfers) and the weights
    are gone. Admission stops first, so no request can start between the check and the acknowledgement."""
    try:
        drained = lease.drain(req.epoch, timeout=600.0)
    except StaleLease as e:
        raise HTTPException(409, f"stale_lease: {e}") from e
    if not drained:
        raise HTTPException(409, "GPU work still active")
    if not trellis.unload():
        raise HTTPException(409, "model still in use")
    if torch.cuda.is_available():
        torch.cuda.synchronize()
    return {"loaded": trellis.loaded, "owner_token": req.owner_token, **lease.info()}
