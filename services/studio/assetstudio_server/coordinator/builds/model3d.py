"""model3d build stages: segment (GPU1 BiRefNet, or CPU when the QA mask is reusable) -> sample (GPU1 TRELLIS.2)
-> bake (GPU1 GLB export) -> finalize (CPU: checks on the delivered file, preview).

Each stage is its own task, so several approved 3D items are segmented, then sampled, then baked in grouped passes.
Every stage commits a checkpoint: a failed bake with a durable raw never calls TRELLIS.2 again, and a re-export
reuses the stored raw of an earlier run. Worker executions are reconciled by id.
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
from ..errors import Blocked, Cancelled
from .common import BuildFailed, BuildInput, foreground_mask
from .material import apply_material
from .sizing import apply_final_size

EXPORT_RANGE = (1_000, 2_000_000)  # what the worker accepts
RAW_MIME = "application/x-npz"


def target_triangles(inp: BuildInput) -> dict[str, Any]:
    """Processing target precedence (separate from the advisory validation budget):
    explicit re-export override > explicitly configured parameter > category budget max > recipe default,
    then clamped only to the backend's declared range."""
    tri = ((inp.snap.get("values") or {}).get("budget") or {}).get("triangles") or {}
    overrides = (inp.reexport or {}).get("overrides") or (inp.run.inputs.get("overrides") if inp.run else None) or {}
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


def geometry_policy(inp: BuildInput) -> dict[str, str]:
    """Explicitly set geometry cleanup keys only (override > build profile); empty = worker defaults."""
    geo = (inp.snap.get("build_profile") or {}).get("geometry") or {}
    overrides = (inp.reexport or {}).get("overrides") or (inp.run.inputs.get("overrides") if inp.run else None) or {}
    out: dict[str, str] = {}
    for key in ("small_components", "fill_holes"):
        value = overrides.get(key) or geo.get(key)
        if value:
            out[key] = str(value)
    return out


def _worker(inp: BuildInput) -> tuple[Any, int]:
    w = inp.env.studio.worker3d
    if w is None:
        raise Blocked("no 3D worker configured (WORKER3D_URL)", "worker3d_unconfigured", operator=True)
    return w, inp.env.epoch("worker3d")


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
        raise Blocked("no 3D worker configured (WORKER3D_URL)", "worker3d_unconfigured", operator=True)
    health = w.health()
    exporter = inp.params["exporter"]
    if health.get("reachable") is False:
        raise Blocked("3D worker unreachable", "engine_unavailable")
    if not (health.get("exporters") or {}).get(exporter):
        raise BuildFailed(f"exporter {exporter!r} is not installed in the 3D worker", "exporter_unavailable")
    if geometry_policy(inp) and "geometry_policy.v1" not in (health.get("export_features") or []):
        raise BuildFailed("the 3D worker does not support geometry cleanup policies; rebuild the worker3d image",
                          "worker_feature_missing")


def segment(inp: BuildInput) -> str:
    if (cp := inp.done("segment")) is not None and cp.outputs.get("cutout"):
        return cp.outputs["cutout"]
    cutout = to_png(apply_mask(load_rgb_array(inp.data), foreground_mask(inp, allow_compute=True)))
    art = inp.env.ctx.store.register_artifact(cutout, "cutout", "image/png", lineage=[inp.source.id],
                                              artifact_id=inp.artifact_id("cutout"))
    inp.checkpoint("segment", {"cutout": art.id, **({"mask": m} if (m := inp.meta.get("mask", {}).get(
        "artifact_id")) else {})}, inputs={"source_sha256": inp.source.sha256},
                   identities={"mask": inp.meta.get("mask", {})})
    return art.id


def sample(inp: BuildInput) -> str:
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
    _preflight(inp)
    cutout_id = segment(inp)
    override = inp.run.inputs.get("seed_override") if inp.run else None  # resample: a new seed, recorded
    seed = int(override if override is not None else (inp.bound.get("seed") or 0)) % 2**31
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


def _export_params(inp: BuildInput) -> tuple[dict[str, Any], dict[str, Any]]:
    budget = target_triangles(inp)
    params = {"exporter": inp.params["exporter"], "decimation_target": budget["effective"],
              "texture_size": int(inp.params["texture_size"]), "remesh": bool(inp.params["remesh"]),
              **geometry_policy(inp)}
    return params, budget


def _bake_matches(cp: Any, params: dict[str, Any]) -> bool:
    """An inherited bake (material-only rebuild) is reused only when it was exported with exactly these settings."""
    recorded = {k: v for k, v in cp.settings.items() if k != "budget"}
    return recorded == params


def bake(inp: BuildInput) -> str:
    params, budget = _export_params(inp)
    if (cp := inp.done("bake")) is not None and _bake_matches(cp, params):
        return cp.outputs["model"]
    raw_id = sample(inp) if inp.reexport else inp.done("sample").outputs["raw"]  # type: ignore[union-attr]
    _preflight(inp)  # also when the sample was inherited (rebuild): the worker must support these settings now
    exporter = inp.params["exporter"]
    store = inp.env.ctx.store
    glb, meta, eid = _execute(inp, "bake", "export", params, store.artifact_bytes(raw_id))
    model_id = store.register_artifact(glb, "model", "model/gltf-binary", lineage=[raw_id],
                                       meta={"exporter": exporter}, source={"export": meta},
                                       artifact_id=inp.artifact_id("model", eid)).id
    inp.roles["raw"] = raw_id
    inp.checkpoint("bake", {"model": model_id}, inputs={"raw": raw_id}, settings={**params, "budget": budget},
                   identities={"exporter": exporter, "licence": meta.get("licence")},
                   receipt={**meta, "execution_id": eid})
    inp.env.studio.worker3d.ack(eid)  # type: ignore[union-attr]
    return model_id


def finalize(inp: BuildInput) -> None:
    """CPU: every required check runs on the delivered GLB bytes (verified read); preview after, isolated."""
    info = inspect_image(inp.data, ("PNG", "JPEG"))
    inp.check("decode", True, f"{info.format} {info.width}x{info.height}")
    bake_cp, sample_cp = inp.done("bake"), inp.done("sample")
    assert bake_cp is not None
    model_id, glb = apply_final_size(inp, apply_material(inp, bake_cp.outputs["model"]))
    budget = bake_cp.settings.get("budget") or target_triangles(inp)
    _validate(inp, glb, budget)
    inp.roles["model"] = model_id
    inp.roles["raw"] = bake_cp.inputs["raw"]
    inp.preview = lambda: preview_png(glb)
    reused = inp.reexport["from_run"].id if inp.reexport else None
    inp.meta.update(export=bake_cp.receipt, budget=budget,
                    generation={"reused_raw_from": reused} if reused else (sample_cp.receipt if sample_cp else {}),
                    mask=((inp.done("segment") or bake_cp).identities or {}).get("mask", {}))
    exporter = bake_cp.settings.get("exporter", inp.params["exporter"])
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
    geo = (inp.snap.get("build_profile") or {}).get("geometry") or {}
    if geo.get("expect_single_component") is False or geometry_policy(inp).get("small_components") == "preserve":
        inp.meta["single_component"] = f"not applicable: {stats['components']} components expected by the build profile"
    else:
        inp.check("single_component", stats["components"] == 1, f"{stats['components']} geometric components",
                  advisory=True)
