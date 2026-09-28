"""Job lifecycle nodes: create, enhance, confirm prompt, select candidate."""
from __future__ import annotations

import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import folder_paths

from . import clients
from .job_io import Job, JobError, can_transition, now_iso
from .settings import OUTPUT_ROOT, app_config, model_pins, read_prompt_file

ASSET_TYPES = ["none", "small_prop", "medium_prop", "large_prop", "rock", "tree_trunk", "plant", "weapon"]
CATEGORY = "line_a"


def load_job(job_id: str) -> Job:
    return Job(OUTPUT_ROOT, job_id.strip())


@contextmanager
def stage(job: Job, fail_state: str) -> Iterator[None]:
    """Mark job failed (preserving artifacts) and re-raise so ComfyUI shows the error."""
    try:
        yield
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        job.log(f"ERROR {msg}")
        if can_transition(job.state, fail_state):
            job.set_state(fail_state, error=msg[:2000])
        raise


class _SideEffectNode:
    CATEGORY = CATEGORY

    @classmethod
    def IS_CHANGED(cls, **_: Any) -> float:
        return float("nan")  # never cache: every run has side effects on disk


def style_loras() -> list[str]:
    """LoRAs from models/loras/style only (speed LoRAs are applied via speed_preset)."""
    names = folder_paths.get_filename_list("loras")
    return [n for n in names if "/loras/speed/" not in (folder_paths.get_full_path("loras", n) or "")]


class LineACreateJob(_SideEffectNode):
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("job_id",)
    FUNCTION = "run"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": "wooden medieval barrel"}),
                "asset_type": (ASSET_TYPES, {"default": "small_prop"}),
                "target_triangles": ("INT", {"default": 0, "min": 0, "max": 10_000_000, "tooltip": "0 = no target"}),
                "candidate_count": ("INT", {"default": 4, "min": 1, "max": 8}),
                "seed_family": ("INT", {"default": 0, "min": 0, "max": 2**31 - 1, "tooltip": "0 = random"}),
                "lora_name": (["none"] + style_loras(), {"default": "none"}),
                "lora_strength": ("FLOAT", {"default": 0.8, "min": -2.0, "max": 2.0, "step": 0.05}),
                "notes": ("STRING", {"default": ""}),
            }
        }

    def run(self, prompt: str, asset_type: str, target_triangles: int, candidate_count: int,
            seed_family: int, lora_name: str, lora_strength: float, notes: str) -> dict:
        if not prompt.strip():
            raise ValueError("prompt is empty")
        if seed_family == 0:
            seed_family = int(datetime.now(timezone.utc).timestamp()) % (2**31 - 1000)
        lora_name = "" if lora_name == "none" else lora_name
        request = {
            "prompt": prompt.strip(),
            "asset_type": None if asset_type == "none" else asset_type,
            "target_triangles": target_triangles or None,
            "candidate_count": candidate_count,
            "lora_name": lora_name or None,
            "lora_strength": lora_strength if lora_name else None,
            "seed_family": seed_family,
            "notes": notes or None,
        }
        job = Job.create(OUTPUT_ROOT, request)
        job.update_manifest(models=model_pins(), config={"app": app_config()})
        return {"ui": {"text": [job.id]}, "result": (job.id,)}


class LineAEnhancePrompt(_SideEffectNode):
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("job_id", "enhanced_prompt")
    FUNCTION = "run"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {"required": {"job_id": ("STRING", {"forceInput": True})}}

    def run(self, job_id: str) -> dict:
        job = load_job(job_id)
        req = job.read_json("request.json")
        with stage(job, "failed_prompt"):
            clients.free_gpu1_for_prompt_service()
            res = clients.enhance(req["prompt"], req.get("asset_type"), req.get("target_triangles"))
            job.write_text("enhanced-prompt.original.txt", res["enhanced_prompt"])
            job.write_json("enhancement.json", res)
            job.update_manifest(prompts={"original": req["prompt"], "enhanced_original": res["enhanced_prompt"],
                                         "enhancer": res["meta"]["model"],
                                         "template_sha256": res["meta"]["template_sha256"]})
            job.set_state("prompt_enhanced")
        return {"ui": {"text": [res["enhanced_prompt"]]}, "result": (job.id, res["enhanced_prompt"])}


class LineAConfirmPrompt(_SideEffectNode):
    """User edit point: empty final_prompt means 'use the enhanced prompt as-is'."""
    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("job_id", "positive", "negative")
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {
            "required": {
                "job_id": ("STRING", {"default": ""}),
                "final_prompt": ("STRING", {"multiline": True, "default": ""}),
            }
        }

    def run(self, job_id: str, final_prompt: str) -> tuple[str, str, str]:
        job = load_job(job_id)
        if job.state != "prompt_enhanced":
            raise JobError(f"job {job.id} is in state {job.state}, expected prompt_enhanced")
        original = job.path("enhanced-prompt.original.txt").read_text()
        final = final_prompt.strip() or original
        negative = read_prompt_file("negative_constraints.txt")
        job.write_text("enhanced-prompt.final.txt", final)
        job.update_manifest(prompts={"final": final, "edited_by_user": final != original, "negative": negative})
        job.set_state("prompt_confirmed")
        return (job.id, final, negative)


class LineASelectCandidate(_SideEffectNode):
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("job_id",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {"required": {"job_id": ("STRING", {"default": ""}), "index": ("INT", {"default": 0, "min": 0, "max": 7})}}

    def run(self, job_id: str, index: int) -> tuple[str]:
        select_candidate(load_job(job_id), index)
        return (job_id.strip(),)


def select_candidate(job: Job, index: int) -> dict:
    """Shared by node and HTTP route. Re-selection archives previous 3D outputs, never deletes."""
    name = f"{index:02d}"
    src = job.path(f"candidates/{name}.png")
    if not src.is_file():
        raise JobError(f"candidate {name} does not exist")
    if not can_transition(job.state, "candidate_selected"):
        raise JobError(f"cannot select in state {job.state}")
    if job.path("selected").exists():
        archive = job.path(f"attempts/{datetime.now(timezone.utc):%Y%m%d-%H%M%S}")
        for d in ("selected", "cutout", "model"):
            if job.path(d).exists():
                archive.mkdir(parents=True, exist_ok=True)
                shutil.move(job.path(d), archive / d)
        job.log(f"archived previous selection to {archive.name}")
    qa_path = job.path(f"qa/{name}.json")
    qa = job.read_json(f"qa/{name}.json") if qa_path.is_file() else None
    job.path("selected").mkdir()
    shutil.copy2(src, job.path("selected/selected.png"))
    job.write_text("selected/selected_candidate.txt", f"{name}\n")
    if qa is not None:
        job.write_json("selected/selected_qa.json", qa)
    selection = {
        "index": index,
        "candidate": f"{name}.png",
        "selected_at": now_iso(),
        "final_prompt": job.path("enhanced-prompt.final.txt").read_text(),
        "qa_status": qa["status"] if qa else None,
        "qa_reasons": qa["reasons"] if qa else None,
    }
    job.update_manifest(selection=selection)
    job.set_state("candidate_selected")
    return selection
