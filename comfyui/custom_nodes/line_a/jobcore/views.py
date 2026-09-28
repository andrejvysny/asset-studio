"""Read-only job views shared by the ComfyUI routes and the library service. Never writes or locks."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .job_io import Job, JobError

# Studio progress strip (design STAGES): brief, prompt, candidates, QA, review, cut-out, 3D, export, done
STAGE_OF_STATE = {
    "created": 0, "prompt_enhanced": 1, "prompt_confirmed": 2, "candidates_generated": 3,
    "qa_completed": 4, "waiting_for_selection": 4, "candidate_selected": 5, "cutout_completed": 6,
    "model_generated": 7, "postprocessed": 7, "exported": 7, "completed": 8,
    "failed_prompt": 1, "failed_generation": 2, "failed_cutout": 5, "failed_trellis": 6, "failed_postprocess": 7,
}


def _json_or_none(job: Job, rel: str) -> Any:
    p = job.path(rel)
    return job.read_json(rel) if p.is_file() else None


def job_detail(job: Job) -> dict:
    state = job.read_json("job_state.json")
    qa_dir = job.path("qa")
    return {
        "job_id": job.id,
        "state": state["state"],
        "active_operation": state.get("active_operation"),
        "current_attempt": state.get("current_attempt"),
        "history": state["history"],
        "request": job.read_json("request.json"),
        "enhancement": _json_or_none(job, "enhancement.json"),
        "candidate_set": _json_or_none(job, "candidates/set.json"),
        "candidates": sorted(p.stem for p in job.path("candidates").glob("*.png")) if job.path("candidates").is_dir() else [],
        "qa": {p.stem: job.read_json(f"qa/{p.name}") for p in sorted(qa_dir.glob("[0-9][0-9].json"))}
        if qa_dir.is_dir() else {},
        "attempts": job.list_attempts(),
        "manifest": job.read_json("manifest.json"),
    }


def _last_error(history: list[dict]) -> str | None:
    for h in reversed(history):
        if h.get("error"):
            return h["error"]
    return None


def job_summary(job: Job) -> dict:
    state = job.read_json("job_state.json")
    req = job.read_json("request.json")
    enh = _json_or_none(job, "enhancement.json")
    s = state["state"]
    failed = s.startswith("failed_")
    cands = len(list(job.path("candidates").glob("*.png"))) if job.path("candidates").is_dir() else 0
    waiting, action = False, ""
    if state.get("active_operation"):
        action = f"{state['active_operation']['name']} running"
    elif s == "prompt_enhanced":
        waiting, action = True, "Confirm prompt"
    elif s == "waiting_for_selection":
        waiting, action = True, f"Review {cands} candidates"
    elif s == "completed":
        action = "Open attempts"
    elif failed:
        action = (_last_error(state["history"]) or s)[:120]
    return {
        "job_id": job.id,
        "title": (enh or {}).get("short_title") or req.get("prompt", ""),
        "prompt": req.get("prompt", ""),
        "asset_type": req.get("asset_type"),
        "state": s,
        "stage": STAGE_OF_STATE.get(s, 0),
        "waiting": waiting,
        "failed": failed,
        "action": action,
        "active_operation": state.get("active_operation"),
        "current_attempt": state.get("current_attempt"),
        "candidates": cands,
        "created_at": state["history"][0]["at"] if state["history"] else None,
        "updated_at": state["history"][-1]["at"] if state["history"] else None,
    }


def iter_jobs(output_root: Path) -> list[Job]:
    jobs = []
    if output_root.is_dir():
        for d in sorted(output_root.iterdir(), reverse=True):
            if (d / "job_state.json").is_file():
                try:
                    jobs.append(Job(output_root, d.name))
                except (JobError, OSError, ValueError):
                    continue
    return jobs
