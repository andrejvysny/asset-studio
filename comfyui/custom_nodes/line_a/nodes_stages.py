"""QA, cut-out and TRELLIS nodes. Heavy work runs in the GPU1 services; these nodes orchestrate + persist."""
from __future__ import annotations

import numpy as np
import torch
from PIL import Image

from . import clients
from .nodes_job import _SideEffectNode, load_job, stage
from .nodes_review import temp_copy, temp_target
from .qa_rules import evaluate
from .settings import app_config, qa_rules


def _load_image_tensor(path: str) -> torch.Tensor:
    arr = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32) / 255.0
    return torch.from_numpy(arr)[None]


class LineARunQA(_SideEffectNode):
    """Advisory QA: marks candidates recommended / not_recommended; never rejects."""
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("job_id",)
    FUNCTION = "run"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {"required": {"job_id": ("STRING", {"forceInput": True})}}

    def run(self, job_id: str) -> dict:
        job = load_job(job_id)
        rules = qa_rules()
        mrules = rules["mask_checks"]
        prompt = job.path("enhanced-prompt.final.txt").read_text()
        candidates = sorted(p.name for p in job.path("candidates").glob("*.png"))
        results = []
        clients.free_gpu1_for_prompt_service()
        for name in candidates:
            stem = name.removesuffix(".png")
            mask_stats = vlm = None
            try:
                mask_stats = clients.cutout(
                    job.id, f"candidates/{name}", f"qa/cutouts/{name}", f"qa/masks/{name}",
                    alpha_threshold=mrules["mask_not_cropped"]["alpha_threshold"],
                    border_px=mrules["mask_not_cropped"]["border_px"],
                )["stats"]
            except clients.ServiceError as e:  # QA is advisory: record, continue
                job.log(f"QA mask {stem} failed: {e}")
            try:
                vlm = clients.vlm_qa(f"{job.id}/candidates/{name}", prompt)
            except clients.ServiceError as e:
                job.log(f"QA vlm {stem} failed: {e}")
            res = evaluate(name, vlm, mask_stats, rules)
            job.write_json(f"qa/{stem}.json", res)
            results.append(res)
        summary = {
            "mode": app_config()["pipeline"]["qa_mode"],
            "recommended": [r["candidate"] for r in results if r["recommended"]],
            "not_recommended": [r["candidate"] for r in results if not r["recommended"]],
            "candidates": {r["candidate"]: {"status": r["status"], "reasons": r["reasons"]} for r in results},
        }
        job.write_json("qa/summary.json", summary)
        job.update_manifest(qa=summary)
        job.set_state("qa_completed")
        job.set_state("waiting_for_selection")
        lines = [f"{r['candidate']}: {r['status'].upper()}" + (f" - {'; '.join(r['reasons'][:3])}" if r["reasons"] else "")
                 for r in results]
        return {"ui": {"text": ["\n".join(lines)]}, "result": (job.id,)}


class LineACutout(_SideEffectNode):
    RETURN_TYPES = ("STRING", "IMAGE")
    RETURN_NAMES = ("job_id", "cutout")
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {"required": {"job_id": ("STRING", {"forceInput": True})}}

    def run(self, job_id: str) -> tuple[str, torch.Tensor]:
        job = load_job(job_id)
        with stage(job, "failed_cutout"):
            clients.free_gpu1_for_trellis()
            stats = clients.cutout(job.id, "selected/selected.png", "cutout/cutout.png", "cutout/cutout_mask.png")["stats"]
            out = job.path("cutout/cutout.png")
            img = Image.open(out)
            if img.mode != "RGBA":
                raise RuntimeError("cut-out has no alpha channel")
            if stats["fill_ratio"] < 0.01:
                raise RuntimeError(f"cut-out mask nearly empty (fill {stats['fill_ratio']})")
            if stats["touches_border"]:
                job.log("WARNING cut-out touches image border; object may be cropped")
            job.write_json("cutout/cutout_stats.json", stats)
            job.update_manifest(cutout=stats)
            job.set_state("cutout_completed")
        return (job.id, _load_image_tensor(str(out)))


class LineATrellis3D(_SideEffectNode):
    RETURN_TYPES = ("STRING", "IMAGE")
    RETURN_NAMES = ("glb_path", "preview")
    FUNCTION = "run"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        mesh = app_config()["mesh"]
        return {
            "required": {
                "job_id": ("STRING", {"forceInput": True}),
                "model_seed": ("INT", {"default": 42, "min": 0, "max": 2**31 - 1}),
                "pipeline_type": (["1024_cascade", "512", "1024", "1536_cascade"], {"default": "1024_cascade"}),
                "texture_size": ("INT", {"default": mesh["texture_size"], "min": 512, "max": 4096, "step": 512}),
            }
        }

    def run(self, job_id: str, model_seed: int, pipeline_type: str, texture_size: int) -> dict:
        job = load_job(job_id)
        mesh_cfg = app_config()["mesh"]
        target = job.read_json("request.json").get("target_triangles")
        # Target is advisory; clamp to a floor so tiny budgets do not destroy the shape.
        decimation = max(int(target), mesh_cfg["triangle_floor"]) if target else mesh_cfg["default_decimation_target"]
        with stage(job, "failed_trellis"):
            clients.free_gpu1_for_trellis()
            res = clients.trellis_generate(
                job.id, seed=model_seed, pipeline_type=pipeline_type, decimation_target=decimation,
                texture_size=texture_size, min_component_ratio=mesh_cfg["min_component_ratio"],
            )
            job.set_state("model_generated")
            job.set_state("postprocessed")
            job.set_state("exported")
        info = res["mesh_info"]
        job.update_manifest(
            trellis={"params": res["params"], "timings": res["timings"], "preview_error": res["preview_error"],
                     "target_triangles": target, "decimation_target": decimation},
            mesh=info,
            outputs={"glb": res["glb"], "preview": res["preview"], "textures": res["textures"],
                     "mesh_info": "model/processed/mesh_info.json"},
        )
        job.set_state("completed")
        preview_path = job.path(res["preview"]) if res["preview"] else job.path("cutout/cutout.png")
        md, report = temp_target(job, "model_info.md")
        md.write_text("\n".join([
            "# 3D model ready", f"`output/{job.id}/{res['glb']}`", "",
            f"- triangles: **{info['triangles']}** (target {target or '—'}, decimation {decimation})",
            f"- vertices: {info['vertices']}, components: {info['components']} "
            f"(floaters removed: {info['removed_floater_components']})",
            f"- textures: {', '.join(res['textures']) or 'none'}, file size: {info['file_size_bytes'] / 1e6:.1f} MB",
            f"- timings: {res['timings']}",
        ]))
        ui = {
            "3d": [temp_copy(job.path(res["glb"]), job, "model.glb")],
            "images": [temp_copy(preview_path, job, "model_preview.png")],
            "reports": [report],
        }
        return {"ui": ui, "result": (str(job.path(res["glb"])), _load_image_tensor(str(preview_path)))}
