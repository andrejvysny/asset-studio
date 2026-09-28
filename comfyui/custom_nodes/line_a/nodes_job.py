"""Job lifecycle nodes: create, enhance (stops at prompt_enhanced), confirm edited prompt."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Any, Iterator

import folder_paths

from . import clients
from .jobcore.job_io import Job, can_transition, random_seed_family
from .jobcore.prompting import compose_effective, split_template
from .settings import OUTPUT_ROOT, app_config, model_pins, read_prompt_file
from .ui_files import temp_markdown

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
        lora = None if lora_name == "none" else lora_name
        request = {
            "prompt": prompt.strip(),
            "asset_type": None if asset_type == "none" else asset_type,
            "target_triangles": target_triangles or None,
            "candidate_count": candidate_count,
            "lora_name": lora,
            "lora_strength": lora_strength if lora else None,
            "seed_family": seed_family or random_seed_family(),
            "seed_family_random": seed_family == 0,
            "notes": notes or None,
        }
        job = Job.create(OUTPUT_ROOT, request)
        job.update_manifest(models=model_pins(), config={"app": app_config()})
        return {"ui": {"text": [job.id]}, "result": (job.id,)}


class LineAEnhancePrompt(_SideEffectNode):
    """Runs the enhancer and stops the job at prompt_enhanced; nothing is generated here."""
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("job_id", "description")
    FUNCTION = "run"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {"required": {"job_id": ("STRING", {"forceInput": True})}}

    def run(self, job_id: str) -> dict:
        job = load_job(job_id)
        req = job.read_json("request.json")
        template = read_prompt_file("model_sheet_template.txt")
        with job.operation("enhance", "created"), stage(job, "failed_prompt"):
            clients.free_gpu1_for_prompt_service()
            res = clients.enhance(req["prompt"], req.get("asset_type"), req.get("target_triangles"))
            description = split_template(res["enhanced_prompt"], template)
            job.write_text("enhanced-prompt.original.txt", res["enhanced_prompt"])
            job.write_json("enhancement.json", {**res, "description": description})
            job.update_manifest(prompts={"original": req["prompt"], "enhanced_original": res["enhanced_prompt"],
                                         "description_original": description, "enhancer": res["meta"]["model"],
                                         "template_sha256": _sha(template)})
            job.set_state("prompt_enhanced")
        card = temp_markdown(job, "enhanced.md", "\n".join([
            f"# {req['prompt']}", f"**Job id (paste into 'Confirm & Generate'):** `{job.id}`", "",
            "**Enhanced description** (edit a copy of this; the model-sheet template is appended automatically):",
            "", description, "", f"_Template:_ {template}"]))
        return {"ui": {"text": [description], "reports": [card]}, "result": (job.id, description)}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class LineAConfirmPrompt(_SideEffectNode):
    """Human prompt gate. edited_description empty = keep enhancer's description. Template always appended."""
    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("job_id", "positive", "negative")
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {
            "required": {
                "job_id": ("STRING", {"default": ""}),
                "edited_description": ("STRING", {"multiline": True, "default": ""}),
            }
        }

    def run(self, job_id: str, edited_description: str) -> tuple[str, str, str]:
        job = load_job(job_id)
        template = read_prompt_file("model_sheet_template.txt")
        negative = read_prompt_file("negative_constraints.txt")
        with job.locked():
            job.require_state("prompt_enhanced")
            original = job.read_json("enhancement.json")["description"]
            edited = edited_description.strip() or original
            effective = compose_effective(edited, template)
            job.write_text("enhanced-prompt.edited.txt", edited)
            job.write_text("enhanced-prompt.final.txt", effective)
            job.update_manifest(prompts={"user_edited": edited, "edited_by_user": edited != original,
                                         "effective": effective, "negative": negative,
                                         "template_sha256": _sha(template)})
            job.set_state("prompt_confirmed")
        return (job.id, effective, negative)
