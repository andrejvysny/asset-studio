"""QA, cut-out, TRELLIS and re-export nodes. Heavy work runs in the GPU1 services; these nodes orchestrate + persist.

3D outputs live in model/attempts/att-NN/ (immutable per attempt); the job manifest points at the current attempt.
"""
from __future__ import annotations

import numpy as np
import torch
from PIL import Image

from . import clients
from .jobcore.approval import require_current_attempt
from .jobcore.job_io import Job, can_transition
from .jobcore.qa_rules import evaluate
from .nodes_job import _SideEffectNode, load_job, stage
from .settings import app_config, qa_rules
from .ui_files import temp_copy, temp_target

POSTPROCESS_STAGES = {"export", "validation", "postprocess"}  # raw exists: resumable without resampling


def _load_image_tensor(path: str) -> torch.Tensor:
    arr = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32) / 255.0
    return torch.from_numpy(arr)[None]


class LineARunQA(_SideEffectNode):
    """Advisory QA with explicit coverage: recommended / not_recommended / unverified. Never rejects."""
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("job_id",)
    FUNCTION = "run"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {"required": {"job_id": ("STRING", {"forceInput": True})}}

    def run(self, job_id: str) -> dict:
        job = load_job(job_id)
        with job.operation("qa", "candidates_generated"):
            results = self._run_checks(job)
            summary = {
                "mode": app_config()["pipeline"]["qa_mode"],
                "candidate_set": job.read_json("candidates/set.json")["set_id"],
                "candidates": {r["candidate"]: {"status": r["status"], "reasons": r["reasons"],
                                                "warnings": r["warnings"], "coverage": r["coverage"]} for r in results},
            }
            job.write_json("qa/summary.json", summary)
            job.update_manifest(qa=summary)
            job.set_state("qa_completed")
            job.set_state("waiting_for_selection")
        lines = [f"{r['candidate']}: {r['status'].upper()} ({r['coverage']['ran']}/{r['coverage']['total']})" for r in results]
        return {"ui": {"text": ["\n".join(lines)]}, "result": (job.id,)}

    @staticmethod
    def _run_checks(job: Job) -> list[dict]:
        rules = qa_rules()
        mrules = rules["mask_checks"]["mask_not_cropped"]
        prompt = job.path("enhanced-prompt.final.txt").read_text()
        clients.free_gpu1_for_prompt_service()
        results = []
        for path in sorted(job.path("candidates").glob("*.png")):
            name = path.name
            mask_stats = vlm = mask_err = vlm_err = None
            try:
                mask_stats = clients.cutout(job.id, f"candidates/{name}", f"qa/cutouts/{name}", f"qa/masks/{name}",
                                            alpha_threshold=mrules["alpha_threshold"], border_px=mrules["border_px"])["stats"]
            except clients.ServiceError as e:  # advisory: record, continue
                mask_err = str(e)[:300]
                job.log(f"QA mask {name} failed: {e}")
            try:
                vlm = clients.vlm_qa(f"{job.id}/candidates/{name}", prompt)
            except clients.ServiceError as e:
                vlm_err = str(e)[:300]
                job.log(f"QA vlm {name} failed: {e}")
            res = evaluate(name, vlm, mask_stats, rules, vlm_error=vlm_err, mask_error=mask_err)
            job.write_json(f"qa/{path.stem}.json", res)
            results.append(res)
        return results


class LineACutout(_SideEffectNode):
    """Always-on BiRefNet cut-out for the current (approved) attempt."""
    RETURN_TYPES = ("STRING", "STRING", "IMAGE")
    RETURN_NAMES = ("job_id", "attempt_id", "cutout")
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {"required": {"job_id": ("STRING", {"default": ""}), "attempt_id": ("STRING", {"default": ""})}}

    def run(self, job_id: str, attempt_id: str) -> tuple[str, str, torch.Tensor]:
        job = load_job(job_id)
        with job.operation("cutout", "candidate_selected"):
            require_current_attempt(job, attempt_id, {"approved"})
            adir = job.attempt_dir(attempt_id)
            with stage(job, "failed_cutout"):
                try:
                    clients.free_gpu1_for_trellis()
                    stats = clients.cutout(job.id, f"{adir}/selected.png", f"{adir}/cutout.png",
                                           f"{adir}/cutout_mask.png")["stats"]
                    if Image.open(job.path(f"{adir}/cutout.png")).mode != "RGBA":
                        raise RuntimeError("cut-out has no alpha channel")
                    if stats["fill_ratio"] < 0.01:
                        raise RuntimeError(f"cut-out mask nearly empty (fill {stats['fill_ratio']})")
                except Exception as e:
                    job.update_attempt(attempt_id, state="failed_cutout", error=f"{type(e).__name__}: {e}"[:2000])
                    raise
                if stats["touches_border"]:
                    job.log("WARNING cut-out touches image border; object may be cropped")
                job.update_attempt(attempt_id, state="cutout_completed", cutout=stats)
                job.set_state("cutout_completed")
        return (job.id, attempt_id, _load_image_tensor(str(job.path(f"{adir}/cutout.png"))))


def _decimation(job: Job) -> tuple[int | None, int, str]:
    """(requested, effective, reason). Target is advisory; a floor keeps tiny budgets from destroying shape."""
    mesh_cfg = app_config()["mesh"]
    target = job.read_json("request.json").get("target_triangles")
    if not target:
        return None, int(mesh_cfg["default_decimation_target"]), "default"
    floor = int(mesh_cfg["triangle_floor"])
    return int(target), max(int(target), floor), "triangle_floor" if int(target) < floor else "requested"


def _cleanup_flags(remesh: bool, drop_floaters: bool) -> dict:
    return {"remesh": remesh, "drop_floaters": drop_floaters,
            "min_component_ratio": float(app_config()["mesh"]["min_component_ratio"])}


def _advance_job_to_completed(job: Job) -> None:
    for s in ("model_generated", "postprocessed", "exported", "completed"):
        if can_transition(job.state, s):
            job.set_state(s)


def _record_success(job: Job, attempt_id: str, res: dict, requested: int | None, effective: int, reason: str) -> dict:
    triangles = {"requested": requested, "decimation_target": effective, "reason": reason,
                 "actual": res["mesh_info"]["triangles"]}
    attempt = job.update_attempt(attempt_id, state="completed", validated=res["validation"]["ok"],
                                 validation=res["validation"], mesh=res["mesh_info"], triangles=triangles,
                                 timings=res["timings"], params=res["params"], outputs=res["outputs"],
                                 known_limitations=res.get("known_limitations", []), error=None)
    job.update_manifest(current_attempt=attempt_id, mesh=res["mesh_info"],
                        outputs={k: f"{job.attempt_dir(attempt_id)}/{v}" if v else None for k, v in res["outputs"].items()})
    return attempt


def _record_failure(job: Job, attempt_id: str, e: clients.ServiceError | Exception) -> str:
    stage_name = getattr(e, "stage", None)
    state = "failed_postprocess" if stage_name in POSTPROCESS_STAGES else "failed_trellis"
    job.update_attempt(attempt_id, state=state, error=str(e)[:2000], failed_stage=stage_name)
    return state


def _ui(job: Job, attempt_id: str, attempt: dict) -> dict:
    adir = job.attempt_dir(attempt_id)
    info, tri = attempt["mesh"], attempt["triangles"]
    md, report = temp_target(job, f"{attempt_id}_info.md")
    md.write_text("\n".join([
        f"# {attempt_id}: 3D model {'validated' if attempt['validated'] else 'NOT validated'}",
        f"`output/{job.id}/{adir}/{attempt['outputs']['glb']}`", "",
        f"- triangles: **{info['triangles']}** (requested {tri['requested'] or '—'}, "
        f"decimation {tri['decimation_target']}, {tri['reason']})",
        f"- vertices {info['vertices']}, components {info['components']}",
        "- validation: " + ", ".join(f"{c['id']}={'ok' if c['ok'] else 'FAIL'}" for c in attempt["validation"]["checks"]),
    ] + [f"- limitation: {x}" for x in attempt.get("known_limitations", [])]))
    return {"3d": [temp_copy(job.path(f"{adir}/{attempt['outputs']['glb']}"), job, f"{attempt_id}_model.glb")],
            "reports": [report]}


class LineATrellis3D(_SideEffectNode):
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("glb_path",)
    FUNCTION = "run"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {
            "required": {
                "job_id": ("STRING", {"forceInput": True}),
                "attempt_id": ("STRING", {"forceInput": True}),
                "model_seed": ("INT", {"default": 42, "min": 0, "max": 2**31 - 1}),
                "pipeline_type": (["1024_cascade", "512", "1024", "1536_cascade"], {"default": "1024_cascade"}),
                "texture_size": ("INT", {"default": app_config()["mesh"]["texture_size"], "min": 512, "max": 4096,
                                         "step": 512}),
                "remesh": ("BOOLEAN", {"default": False}),
                "drop_floaters": ("BOOLEAN", {"default": False}),
            }
        }

    def run(self, job_id: str, attempt_id: str, model_seed: int, pipeline_type: str, texture_size: int,
            remesh: bool, drop_floaters: bool) -> dict:
        job = load_job(job_id)
        requested, effective, reason = _decimation(job)
        with job.operation("trellis", "cutout_completed"):
            require_current_attempt(job, attempt_id, {"cutout_completed"})
            try:
                clients.free_gpu1_for_trellis()
                res = clients.trellis_generate(
                    job.id, attempt_dir=job.attempt_dir(attempt_id), seed=model_seed, pipeline_type=pipeline_type,
                    decimation_target=effective, texture_size=texture_size, cleanup=_cleanup_flags(remesh, drop_floaters))
            except Exception as e:
                state = _record_failure(job, attempt_id, e)
                if state == "failed_postprocess":
                    job.set_state("model_generated")
                with stage(job, state):
                    raise
            attempt = _record_success(job, attempt_id, res, requested, effective, reason)
            _advance_job_to_completed(job)
        return {"ui": _ui(job, attempt_id, attempt), "result": (str(job.path(job.attempt_dir(attempt_id))),)}


class LineAReexport(_SideEffectNode):
    """New attempt from a previous attempt's raw TRELLIS output: re-runs export/cleanup only, no resampling."""
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("attempt_id",)
    FUNCTION = "run"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {
            "required": {
                "job_id": ("STRING", {"default": ""}),
                "from_attempt": ("STRING", {"default": ""}),
                "texture_size": ("INT", {"default": app_config()["mesh"]["texture_size"], "min": 512, "max": 4096,
                                         "step": 512}),
                "remesh": ("BOOLEAN", {"default": False}),
                "drop_floaters": ("BOOLEAN", {"default": False}),
            }
        }

    def run(self, job_id: str, from_attempt: str, texture_size: int, remesh: bool, drop_floaters: bool) -> dict:
        job = load_job(job_id)
        requested, effective, reason = _decimation(job)
        allowed = {"completed", "failed_postprocess", "model_generated", "postprocessed", "exported"}
        with job.operation("reexport", allowed):
            src = job.read_attempt(from_attempt)
            src_dir = job.attempt_dir(from_attempt)
            if not job.path(f"{src_dir}/raw/raw.pt").is_file():
                raise FileNotFoundError(f"{from_attempt} has no raw intermediate to re-export")
            keep = {k: src.get(k) for k in ("candidate_set", "index", "candidate", "image_sha256", "qa_status")}
            attempt = job.new_attempt({**keep, "state": "reexporting", "raw_from": from_attempt,
                                       "cleanup": _cleanup_flags(remesh, drop_floaters)})
            aid = attempt["id"]
            state = job.read_json("job_state.json")
            state["current_attempt"] = aid
            job.write_json("job_state.json", state)
            try:
                res = clients.trellis_reexport(
                    job.id, attempt_dir=job.attempt_dir(aid), from_attempt_dir=src_dir, decimation_target=effective,
                    texture_size=texture_size, cleanup=_cleanup_flags(remesh, drop_floaters))
            except Exception as e:
                job.update_attempt(aid, state="failed_postprocess", error=str(e)[:2000], failed_stage=getattr(e, "stage", None))
                raise
            done = _record_success(job, aid, res, requested, effective, reason)
            _advance_job_to_completed(job)
        return {"ui": _ui(job, aid, done), "result": (aid,)}
