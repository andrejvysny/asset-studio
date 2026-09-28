"""Candidate generation: optional LoRA + N sequential samples saved into the job dir."""
from __future__ import annotations

import json
from typing import Any

import comfy.model_management
import comfy.samplers
import comfy.sd
import comfy.utils
import folder_paths
import nodes
import numpy as np
import torch
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from .job_io import Job
from .nodes_job import CATEGORY, _SideEffectNode, load_job, stage
from .settings import app_config

NONE = "none"


class LineAOptionalLora:
    """Pass-through when lora_name == 'none' so one workflow covers both cases."""
    CATEGORY = CATEGORY
    RETURN_TYPES = ("MODEL",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {
            "required": {
                "model": ("MODEL",),
                "lora_name": ([NONE] + folder_paths.get_filename_list("loras"), {"default": NONE}),
                "strength": ("FLOAT", {"default": 0.8, "min": -2.0, "max": 2.0, "step": 0.05}),
            }
        }

    def run(self, model: Any, lora_name: str, strength: float) -> tuple[Any]:
        if lora_name == NONE or strength == 0:
            return (model,)
        lora = comfy.utils.load_torch_file(folder_paths.get_full_path_or_raise("loras", lora_name), safe_load=True)
        model_lora, _ = comfy.sd.load_lora_for_models(model, None, lora, strength, 0)
        return (model_lora,)


def _to_pil(image: torch.Tensor) -> Image.Image:
    arr = np.clip(image.cpu().numpy() * 255.0, 0, 255).astype(np.uint8)
    return Image.fromarray(arr)


class LineAGenerateCandidates(_SideEffectNode):
    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "job_id")
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        img = app_config()["image"]
        return {
            "required": {
                "job_id": ("STRING", {"forceInput": True}),
                "model": ("MODEL",),
                "vae": ("VAE",),
                "positive": ("CONDITIONING",),
                "negative": ("CONDITIONING",),
                "width": ("INT", {"default": img["width"], "min": 256, "max": 2048, "step": 16}),
                "height": ("INT", {"default": img["height"], "min": 256, "max": 2048, "step": 16}),
                "steps": ("INT", {"default": img["steps"], "min": 1, "max": 150}),
                "cfg": ("FLOAT", {"default": img["cfg"], "min": 0.0, "max": 20.0, "step": 0.1}),
                "sampler_name": (comfy.samplers.KSampler.SAMPLERS, {"default": img["sampler"]}),
                "scheduler": (comfy.samplers.KSampler.SCHEDULERS, {"default": img["scheduler"]}),
                "speed_preset": (["quality"] + list(img.get("speed_presets", {})), {
                    "default": "quality", "tooltip": "lightning_* applies the Lightning LoRA and overrides steps/cfg"}),
            }
        }

    def run(self, job_id: str, model: Any, vae: Any, positive: Any, negative: Any, width: int, height: int,
            steps: int, cfg: float, sampler_name: str, scheduler: str, speed_preset: str) -> tuple[torch.Tensor, str]:
        job = load_job(job_id)
        req = job.read_json("request.json")
        if req.get("lora_name"):
            (model,) = LineAOptionalLora().run(model, req["lora_name"], float(req.get("lora_strength") or 0.8))
        if speed_preset != "quality":
            preset = app_config()["image"]["speed_presets"][speed_preset]
            (model,) = LineAOptionalLora().run(model, preset["lora"], 1.0)
            steps, cfg = int(preset["steps"]), float(preset["cfg"])
        count = int(req.get("candidate_count") or app_config()["pipeline"]["default_candidate_count"])
        seed_family = int(req["seed_family"])
        params = {"width": width, "height": height, "steps": steps, "cfg": cfg, "sampler": sampler_name,
                  "scheduler": scheduler, "speed_preset": speed_preset, "count": count,
                  "lora_name": req.get("lora_name"), "lora_strength": req.get("lora_strength")}
        job.update_manifest(generation=params)
        images: list[torch.Tensor] = []
        seeds: list[int] = []
        with stage(job, "failed_generation"):
            for i in range(count):
                seed = seed_family + i
                images.append(self._sample_one(job, i, seed, model, vae, positive, negative, params))
                seeds.append(seed)
                job.update_manifest(seeds=seeds)  # keep partial progress if a later sample fails
            job.set_state("candidates_generated")
        return (torch.cat(images, dim=0), job.id)

    @staticmethod
    def _sample_one(job: Job, i: int, seed: int, model: Any, vae: Any, positive: Any, negative: Any,
                    p: dict) -> torch.Tensor:
        latent = torch.zeros([1, 16, p["height"] // 8, p["width"] // 8],
                             device=comfy.model_management.intermediate_device())
        (out,) = nodes.common_ksampler(model, seed, p["steps"], p["cfg"], p["sampler"], p["scheduler"],
                                       positive, negative, {"samples": latent})
        image = vae.decode(out["samples"])
        if image.ndim == 5:  # some VAEs return (B, T, H, W, C)
            image = image.reshape(-1, *image.shape[-3:])
        meta = PngInfo()
        meta.add_text("line_a", json.dumps({"job_id": job.id, "index": i, "seed": seed, **p}))
        path = job.path(f"candidates/{i:02d}.png")
        path.parent.mkdir(parents=True, exist_ok=True)
        _to_pil(image[0]).save(path, pnginfo=meta)
        job.log(f"candidate {i:02d} saved (seed {seed})")
        return image[:1]
