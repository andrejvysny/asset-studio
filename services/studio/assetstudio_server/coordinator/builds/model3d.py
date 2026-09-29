"""model3d build: approved image -> BiRefNet cut-out -> TRELLIS.2 raw (stored) -> GLB export -> checks -> preview.

Re-export reuses the stored raw intermediate of an earlier run and never resamples TRELLIS.2.
"""
from __future__ import annotations

from typing import Any

from assetstudio_processing.glb import validate_glb_bytes
from assetstudio_processing.images import inspect_image, load_rgb_array
from assetstudio_processing.raster import apply_mask, to_png
from assetstudio_processing.render import glb_stats, preview_png

from ...adapters.base import EngineRejected, EngineUnavailable
from .common import BuildFailed, BuildInput, foreground_mask

EXPORT_RANGE = (1_000, 2_000_000)  # what the worker accepts
RAW_MIME = "application/x-npz"


def _budget(inp: BuildInput) -> dict[str, Any]:
    """requested (category budget or recipe default) -> effective (clamped to the exporter range)."""
    tri = ((inp.snap.get("values") or {}).get("budget") or {}).get("triangles") or {}
    requested = tri.get("max") or int(inp.params["triangles"])
    effective = max(EXPORT_RANGE[0], min(EXPORT_RANGE[1], int(requested)))
    return {"min": tri.get("min"), "max": tri.get("max"), "advisory": tri.get("advisory", True),
            "requested": int(requested), "effective": effective,
            "source": "category budget" if tri.get("max") else "recipe parameter"}


def _worker(inp: BuildInput) -> Any:
    w = inp.env.studio.worker3d
    if w is None:
        raise BuildFailed("no 3D worker configured (WORKER3D_URL)")
    inp.env.studio.lanes["gpu1"].acquire("worker3d")
    return w


def _raw(inp: BuildInput) -> tuple[bytes, str]:
    store = inp.env.ctx.store
    if inp.reexport:
        raw_id = inp.reexport["from_run"].artifacts.get("raw")
        if not raw_id:
            raise BuildFailed("the source build has no stored raw intermediate")
        inp.meta["generation"] = {"reused_raw_from": inp.reexport["from_run"].id}
        return store.artifact_bytes(raw_id), raw_id
    cutout = to_png(apply_mask(load_rgb_array(inp.data), foreground_mask(inp)))
    inp.roles["cutout"] = store.register_artifact(cutout, "cutout", "image/png", lineage=[inp.source.id]).id
    seed = int(inp.bound.get("seed") or 0) % 2**31
    try:
        raw, meta = _worker(inp).generate(image_rgba=cutout, seed=seed, pipeline_type=inp.params["pipeline_type"])
    except (EngineUnavailable, EngineRejected) as e:
        raise BuildFailed(f"TRELLIS.2 generation failed: {e}") from e
    art = store.register_artifact(raw, "raw", RAW_MIME, lineage=[inp.roles["cutout"]], retention="raw",
                                  meta={"format": meta.get("raw_format"), "faces": meta.get("raw_faces")},
                                  source={"engine": meta})
    inp.meta["generation"] = meta
    return raw, art.id


def model3d(inp: BuildInput) -> None:
    info = inspect_image(inp.data, ("PNG", "JPEG"))
    inp.check("decode", True, f"{info.format} {info.width}x{info.height}")
    raw, raw_id = _raw(inp)
    inp.roles["raw"] = raw_id
    budget = _budget(inp)
    exporter = inp.params["exporter"]
    try:
        glb, meta = _worker(inp).export(raw=raw, exporter=exporter, decimation_target=budget["effective"],
                                        texture_size=int(inp.params["texture_size"]),
                                        remesh=bool(inp.params["remesh"]))
    except (EngineUnavailable, EngineRejected) as e:
        raise BuildFailed(f"GLB export failed: {e}") from e
    store = inp.env.ctx.store
    inp.roles["model"] = store.register_artifact(glb, "model", "model/gltf-binary", lineage=[raw_id],
                                                 meta={"exporter": exporter}, source={"export": meta}).id
    _validate(inp, glb, budget)
    inp.roles["preview"] = store.register_artifact(preview_png(glb), "preview", "image/png",
                                                   lineage=[inp.roles["model"]],
                                                   meta={"derived": "CPU render, 4 views"}).id
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
    inp.check("triangle_budget", within, f"{tri} triangles (requested ≤{budget['requested']}, "
              f"effective {budget['effective']}{f', min {lo}' if lo else ''})", advisory=bool(budget["advisory"]))
    inp.check("single_component", stats["components"] == 1, f"{stats['components']} geometric components",
              advisory=True)
