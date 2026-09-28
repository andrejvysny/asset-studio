"""ComfyUI temp copies for App Mode outputs. Job dirs are outside ComfyUI's output dir, so /view cannot serve
them directly; these are display copies, the job dir stays the source of truth."""
from __future__ import annotations

import shutil
from pathlib import Path

import folder_paths

from .jobcore.job_io import Job


def temp_target(job: Job, name: str) -> tuple[Path, dict]:
    sub = f"line_a/{job.id}"
    dst = Path(folder_paths.get_temp_directory()) / sub / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    return dst, {"filename": name, "subfolder": sub, "type": "temp"}


def temp_copy(src: Path, job: Job, name: str) -> dict:
    dst, item = temp_target(job, name)
    shutil.copyfile(src, dst)
    return item


def temp_markdown(job: Job, name: str, text: str) -> dict:
    dst, item = temp_target(job, name)
    dst.write_text(text)
    return item
