"""model3d build: approved image -> cut-out -> TRELLIS.2 raw -> GLB bake -> checks on the delivered file -> preview.

Every stage commits a checkpoint. Recovery resumes the missing stage: a failed bake with a durable raw never calls
TRELLIS.2 again, and a re-export reuses the stored raw of an earlier run. Worker executions are reconciled by id.
"""
from __future__ import annotations

from typing import Any

from assetstudio_core.recipes import legacy_variant
from assetstudio_processing import raw_npz
from assetstudio_processing.glb import validate_glb_bytes
from assetstudio_processing.images import inspect_image, load_rgb_array
from assetstudio_processing.raster import apply_mask, to_png
from assetstudio_processing.render import glb_stats, preview_png

from ...adapters.base import EngineUnavailable, ExecutionCancelled, ExecutionFailed, ExecutionLost
from ..runner import Blocked, Cancelled
from .common import BuildFailed, BuildInput, foreground_mask

EXPORT_RANGE = (1_000, 2_000_000)  # what the worker accepts
RAW_MIME = "application/x-npz"


def target_triangles(inp: BuildInput) -> dict[str, Any]:
    """Processing target precedence (separate from the advisory validation budget):
    explicit re-export override > explicitly configured parameter > category budget max > recipe default,
    then clamped only to the backend's declared range."""
    tri = ((inp.snap.get("values") or {}).get("budget") or {}).get("triangles") or {}
    overrides = (inp.reexport or {}).get("overrides", {})
    sources = inp.snap.get("parameter_sources") or {}
    if "triangles" in overrides:
        requested, source = int(overrides["triangles"]), "re-export override"
    elif str(sources.get("triangles", "recipe")) != "recipe":
        requested, source = int(inp.params["triangles"]), f"configured ({sources['triangles']})"
    elif tri.get("max"):
        requested, source = int(tri["max"]), "category budget maximum"
    else:
        requested, source = int(inp.params["triangles"]), "recipe default"
    effective = max(EXPORT_RANGE[0], min(EXPORT_RANGE[1], requested))
    return {"min": tri.get("min"), "max": tri.get("max"), "advisory": tri.get("advisory", True),
            "requested": requested, "effective": effective, "source": source,
            "clamp": None if effective == requested else f"clamped to the exporter range {EXPORT_RANGE}"}


def _worker(inp: BuildInput) -> tuple[Any, int]:
    w = inp.env.studio.worker3d
    if w is None:
        raise BuildFailed("no 3D worker configured (WORKER3D_URL)", "resource_unavailable")
    return w, inp.env.studio.lanes["gpu1"].acquire("worker3d")


def _execute(inp: BuildInput, stage: str, op: str, params: dict[str, Any], body: bytes) -> tuple[bytes, dict, str]:
    w, epoch = _worker(inp)
    eid = inp.execution_id(stage)

    def cancelled() -> bool:
        try:
            inp.env.check_cancel()
            return False
        except Cancelled:
            return True
    try:
        data, meta = w.execute(eid, op, params, body, epoch=epoch, should_cancel=cancelled)
    except ExecutionCancelled as e:
        raise Cancelled() from e
    except ExecutionLost as e:
        raise BuildFailed(f"{stage}: worker restarted mid-execution ({e}); retry creates a new attempt",
                          "lost_execution") from e
    except ExecutionFailed as e:
        raise BuildFailed(f"{stage}: {e}", e.code) from e
    except EngineUnavailable as e:  # uncertain: keep the id, reconcile on the next attempt
        raise Blocked(f"{stage}: 3D worker unavailable ({e})", "engine_unavailable") from e
    return data, meta, eid


def _preflight(inp: BuildInput) -> None:
    """Resolve the selected execution closure BEFORE any expensive stage (no late exporter discovery)."""
    if (variant := legacy_variant(inp.snap)) is not None:
        raise BuildFailed(f"legacy recipe snapshot ({variant}): parameters changed meaning; fork the Job to the "
                          "current recipe before building", "legacy_recipe")
    w = inp.env.studio.worker3d
    if w is None:
        raise BuildFailed("no 3D worker configured (WORKER3D_URL)", "resource_unavailable")
    health = w.health()
    exporter = inp.params["exporter"]
    if health.get("reachable") is False:
        raise Blocked("3D worker unreachable", "engine_unavailable")
    if not (health.get("exporters") or {}).get(exporter):
        raise BuildFailed(f"exporter {exporter!r} is not installed in the 3D worker", "exporter_unavailable")


def _segment(inp: BuildInput) -> str:
    if (cp := inp.done("segment")) is not None:
        return cp.outputs["cutout"]
    cutout = to_png(apply_mask(load_rgb_array(inp.data), foreground_mask(inp)))
    art = inp.env.ctx.store.register_artifact(cutout, "cutout", "image/png", lineage=[inp.source.id],
                                              artifact_id=inp.artifact_id("cutout"))
    inp.checkpoint("segment", {"cutout": art.id}, inputs={"source_sha256": inp.source.sha256},
                   identities={"mask": inp.meta.get("mask", {})})
    return art.id


def _sample(inp: BuildInput) -> str:
    """Stage output: a verified, durable raw intermediate. Reused from a checkpoint or an earlier run."""
    store = inp.env.ctx.store
    if inp.reexport:
        raw_id = inp.reexport["from_run"].artifacts.get("raw")
        if not raw_id:
            raise BuildFailed("the source build has no stored raw intermediate", "input_invalid")
        inp.meta["generation"] = {"reused_raw_from": inp.reexport["from_run"].id}
        return raw_id
    if (cp := inp.done("sample")) is not None:
        inp.meta["generation"] = {**cp.receipt, "resumed_from_checkpoint": True}
        return cp.outputs["raw"]
    cutout_id = _segment(inp)
    seed = int(inp.bound.get("seed") or 0) % 2**31
    params = {"seed": seed, "pipeline_type": inp.params["pipeline_type"]}
    raw, meta, eid = _execute(inp, "sample", "generate", params, store.artifact_bytes(cutout_id))
    if not raw.startswith(b"SIMULATED-RAW:"):
        try:
            raw_npz.load(raw)  # ingest-time validation: never store an intermediate the exporter would reject
        except raw_npz.RawInvalid as e:
            raise BuildFailed(f"raw intermediate from the worker is invalid: {e}", "output_invalid") from e
    art = store.register_artifact(raw, "raw", RAW_MIME, lineage=[cutout_id], retention="raw",
                                  meta={"format": meta.get("raw_format"), "faces": meta.get("raw_faces")},
                                  source={"engine": meta}, artifact_id=inp.artifact_id("raw", eid))
    inp.checkpoint("sample", {"raw": art.id}, inputs={"cutout": cutout_id}, settings=params,
                   identities={"engine": meta.get("engine"), "trellis_ref": meta.get("trellis_ref"),
                               "worker_session": meta.get("worker_session")},
                   receipt={**meta, "execution_id": eid})
    inp.env.studio.worker3d.ack(eid)  # type: ignore[union-attr]
    inp.meta["generation"] = meta
    return art.id


def model3d(inp: BuildInput) -> None:
    info = inspect_image(inp.data, ("PNG", "JPEG"))
    inp.check("decode", True, f"{info.format} {info.width}x{info.height}")
    _preflight(inp)
    raw_id = _sample(inp)
    inp.roles["raw"] = raw_id
    budget = target_triangles(inp)
    exporter = inp.params["exporter"]
    store = inp.env.ctx.store
    if (cp := inp.done("bake")) is not None:
        model_id, meta = cp.outputs["model"], cp.receipt
    else:
        params = {"exporter": exporter, "decimation_target": budget["effective"],
                  "texture_size": int(inp.params["texture_size"]), "remesh": bool(inp.params["remesh"])}
        glb, meta, eid = _execute(inp, "bake", "export", params, store.artifact_bytes(raw_id))
        model_id = store.register_artifact(glb, "model", "model/gltf-binary", lineage=[raw_id],
                                           meta={"exporter": exporter}, source={"export": meta},
                                           artifact_id=inp.artifact_id("model", eid)).id
        inp.checkpoint("bake", {"model": model_id}, inputs={"raw": raw_id}, settings=params,
                       identities={"exporter": exporter, "licence": meta.get("licence")},
                       receipt={**meta, "execution_id": eid})
        inp.env.studio.worker3d.ack(eid)  # type: ignore[union-attr]
    inp.roles["model"] = model_id
    glb = store.artifact_bytes(model_id)  # validate the delivered file itself (verified read)
    _validate(inp, glb, budget)
    inp.preview = lambda: preview_png(glb)
    inp.meta.update(export=meta, budget=budget)
    inp.components = ["birefnet", "trellis2", "trellis_image_large", "dinov3_vitl16", "trellis2_runtime",
                      "exporter_clean" if exporter == "clean" else "nvdiffrast"]


def _validate(inp: BuildInput, glb: bytes, budget: dict[str, Any]) -> None:
    v = validate_glb_bytes(glb, require_texture=True)
    for c in v["checks"]:
        inp.check(c["id"], c["ok"], c.get("detail", ""))
    if not v["ok"]:
        return
    stats = glb_stats(glb)
    inp.meta["mesh"] = stats
    tri, lo, hi = stats["triangles"], budget["min"], budget["max"]
    within = (lo is None or tri >= lo) and (hi is None or tri <= hi)
    inp.check("triangle_budget", within, f"{tri} triangles (target {budget['effective']} from {budget['source']}"
              f"{f', advisory {lo}–{hi}' if lo or hi else ''})", advisory=bool(budget["advisory"]))
    inp.check("single_component", stats["components"] == 1, f"{stats['components']} geometric components",
              advisory=True)
