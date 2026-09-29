"""Exact final sizing of a reconstructed variant model. TRELLIS.2 output has no real unit: the row's explicit
`final_height_m` defines it, applied as one deterministic wrapper-node scale on the baked GLB (the unsized bake is
kept on the run as the `model_unsized` intermediate and never published)."""
from __future__ import annotations

from assetstudio_core.variants import TargetHeight
from assetstudio_processing.transforms import TransformRejected, apply_glb_transform

from ...services.records import load_job
from .common import BuildInput


def final_height_m(inp: BuildInput) -> float | None:
    job, _ = load_job(inp.env.ctx.store, inp.batch_id)
    if job.direct or not job.variant:
        return None
    return job.variant.get("final_height_m")


def apply_final_size(inp: BuildInput, baked_id: str) -> tuple[str, bytes]:
    """-> (artifact id delivered as `model`, its bytes). No-op without a row height. Idempotent across retries."""
    store = inp.env.ctx.store
    baked = store.artifact_bytes(baked_id)
    height = final_height_m(inp)
    if height is None:
        return baked_id, baked
    inp.roles["model_unsized"] = baked_id
    if (cp := inp.done("size")) is not None:
        report = cp.receipt
        sized_id = cp.outputs["model"]
        sized = store.artifact_bytes(sized_id)
    else:
        try:
            sized, report = apply_glb_transform(baked, TargetHeight(height_m=height, anchor="bottom_center",
                                                                    units_confirmed=True))
        except TransformRejected as e:
            inp.meta["sizing"] = {"error": f"{e.code}: {e}"[:300]}
            inp.check("final_height", False, f"could not size the model: {e}"[:300])
            return baked_id, baked
        sized_id = store.register_artifact(sized, "model", "model/gltf-binary", lineage=[baked_id],
                                           meta={"sizing": {"height_m": height}},
                                           source={"transform": "target_height"},
                                           artifact_id=inp.artifact_id("model_sized")).id
        inp.checkpoint("size", {"model": sized_id, "model_unsized": baked_id}, inputs={"model": baked_id},
                       settings={"height_m": height, "anchor": "bottom_center"}, receipt=report)
    inp.meta["sizing"] = report
    inp.check("final_height", bool(report.get("height_ok")),
              f"{report['output_height']:.4f} m (target {height} m, tolerance {report['tolerance']:.1e})")
    return sized_id, sized
