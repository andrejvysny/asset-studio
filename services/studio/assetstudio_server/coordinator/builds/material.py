"""CPU material stage of a model3d build: the build profile's material policy applied to the baked GLB (alpha mode and
cutoff, culling, metallic replace, roughness clamp). Runs after bake and before final sizing, with its own checkpoint,
so a material-only rebuild reuses the bake and never calls the 3D worker. Without a policy it is a no-op."""
from __future__ import annotations

from typing import Any

from assetstudio_core.config import MATERIAL_KEYS
from assetstudio_processing.materials import MaterialRejected, apply_material_policy, preservation_checks

from .common import BuildInput


def material_policy(inp: BuildInput) -> dict[str, Any]:
    """Explicitly set material keys only: rebuild override > build profile."""
    mat = (inp.snap.get("build_profile") or {}).get("material") or {}
    overrides = (inp.reexport or {}).get("overrides") or (inp.run.inputs.get("overrides") if inp.run else None) or {}
    out = {k: mat[k] for k in MATERIAL_KEYS if mat.get(k) is not None}
    out |= {k: overrides[k] for k in MATERIAL_KEYS if overrides.get(k) is not None}
    if out.get("alpha_mode") in ("opaque", "blend"):
        out.pop("alpha_cutoff", None)  # an override to opaque/blend drops a profile cutoff instead of failing
    return out


def _checks(inp: BuildInput, policy: dict[str, Any], report: dict[str, Any], proof: list[dict[str, Any]]) -> None:
    inp.check("material_policy", all(c["ok"] for c in proof),
              "; ".join(f"{c['id']}: {'ok' if c['ok'] else 'FAILED'}" for c in proof))
    if "alpha_mode" in policy:
        first = report["materials"][0]
        auto = first.get("auto")
        detail = (f"auto chose {first['alpha_mode']} ({auto['transparent_fraction']:.2%} of texels below the cutoff)"
                  if auto else f"{first['alpha_mode']} as configured")
        inp.check("alpha_mode", all(m.get("alpha_mode") for m in report["materials"]), detail)


def apply_material(inp: BuildInput, baked_id: str) -> str:
    """-> artifact id of the model after the material policy (the baked id itself when there is no policy)."""
    policy = material_policy(inp)
    if not policy:
        return baked_id
    store = inp.env.ctx.store
    if (cp := inp.done("material")) is not None and cp.inputs.get("model") == baked_id \
            and cp.settings.get("policy") == policy:
        inp.roles["model_unmaterialized"] = baked_id
        inp.meta["material"] = cp.receipt
        _checks(inp, policy, cp.receipt, cp.receipt["preservation"])
        return cp.outputs["model"]
    baked = store.artifact_bytes(baked_id)
    try:
        out, report = apply_material_policy(baked, policy)
    except MaterialRejected as e:
        inp.meta["material"] = {"error": f"{e.code}: {e}"[:300], "policy": policy}
        inp.check("material_policy", False, f"could not apply the material policy: {e}"[:300])
        return baked_id
    proof = preservation_checks(baked, out, report["rewritten_views"])
    report["preservation"] = proof
    model_id = store.register_artifact(out, "model", "model/gltf-binary", lineage=[baked_id],
                                       meta={"material_policy": policy}, source={"material": "build_profile"},
                                       artifact_id=inp.artifact_id("model_material")).id
    inp.checkpoint("material", {"model": model_id, "model_unmaterialized": baked_id}, inputs={"model": baked_id},
                   settings={"policy": policy}, receipt=report)
    inp.roles["model_unmaterialized"] = baked_id
    inp.meta["material"] = report
    _checks(inp, policy, report, proof)
    return model_id
