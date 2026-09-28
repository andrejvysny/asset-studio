"""App Mode review/approval: QA-annotated gallery + explicit approve step that gates the 3D stages."""
from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import folder_paths
from comfy_execution.graph_utils import ExecutionBlocker
from PIL import Image, ImageDraw, ImageFont

from .job_io import SELECTABLE, Job, JobError
from .nodes_job import _SideEffectNode, load_job, select_candidate
from .settings import OUTPUT_ROOT

REVIEW_ONLY = "review only (no 3D)"
CANDIDATE_CHOICES = [REVIEW_ONLY] + [f"{i:02d}" for i in range(8)]
TILE = 640
GREEN, ORANGE, BLUE, GRAY = (46, 160, 67), (224, 123, 20), (40, 110, 230), (110, 110, 110)


def resolve_job(job_id: str) -> Job:
    """'latest' (or empty) = newest job that has candidates to review."""
    job_id = job_id.strip()
    if job_id and job_id != "latest":
        return load_job(job_id)
    if OUTPUT_ROOT.is_dir():
        for d in sorted(OUTPUT_ROOT.iterdir(), reverse=True):
            if not (d / "job_state.json").is_file():
                continue
            try:
                job = Job(OUTPUT_ROOT, d.name)
            except JobError:
                continue
            if job.state in SELECTABLE:
                return job
    raise JobError("no job with candidates found; run 'Line A - New Asset' first")


def temp_target(job: Job, name: str) -> tuple[Path, dict]:
    """Path in ComfyUI temp + its /view item. Job dirs are outside ComfyUI's output dir, so /view
    cannot serve them directly; previews are copies, the job dir stays the source of truth."""
    sub = f"line_a/{job.id}"
    dst = Path(folder_paths.get_temp_directory()) / sub / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    return dst, {"filename": name, "subfolder": sub, "type": "temp"}


def temp_copy(src: Path, job: Job, name: str) -> dict:
    dst, item = temp_target(job, name)
    shutil.copyfile(src, dst)
    return item


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow without FreeType sizes
        return ImageFont.load_default()


def render_tile(image_path: Path, label: str, qa: dict | None, selected: bool) -> Image.Image:
    img = Image.open(image_path).convert("RGB").resize((TILE, TILE))
    status = (qa or {}).get("status")
    color = GREEN if status == "recommended" else ORANGE if status == "not_recommended" else GRAY
    badge = {"recommended": "RECOMMENDED", "not_recommended": "NOT RECOMMENDED"}.get(status or "", "NO QA")
    reasons = (qa or {}).get("reasons", [])[:4]
    footer_h = 30 + 26 * sum(len(textwrap.wrap(r, 52)) for r in reasons) if reasons else 0
    canvas = Image.new("RGB", (TILE, TILE + 56 + footer_h), (250, 250, 250))
    canvas.paste(img, (0, 56))
    d = ImageDraw.Draw(canvas)
    d.rectangle([0, 0, TILE, 56], fill=color)
    d.text((16, 12), f"#{label}  {badge}" + ("  - SELECTED" if selected else ""), fill="white", font=_font(28))
    y = TILE + 56 + 10
    for r in reasons:
        for line in textwrap.wrap(r, 52):
            d.text((16, y), f"- {line}" if line == textwrap.wrap(r, 52)[0] else f"  {line}", fill=(40, 40, 40), font=_font(20))
            y += 26
    if selected:
        d.rectangle([0, 0, canvas.width - 1, canvas.height - 1], outline=BLUE, width=10)
    return canvas


def overview(tiles: list[Image.Image], cols: int = 2) -> Image.Image:
    rows = -(-len(tiles) // cols)
    cell_h = max(t.height for t in tiles)
    sheet = Image.new("RGB", (cols * TILE + (cols - 1) * 12, rows * cell_h + (rows - 1) * 12), (30, 30, 30))
    for i, t in enumerate(tiles):
        sheet.paste(t, ((i % cols) * (TILE + 12), (i // cols) * (cell_h + 12)))
    return sheet


def summary_markdown(job: Job, qa_by_name: dict[str, dict], selected: str | None) -> str:
    req = job.read_json("request.json")
    final = job.path("enhanced-prompt.final.txt")
    rows = [
        f"| #{n.removesuffix('.png')} | {'✅ recommended' if q.get('recommended') else '⚠️ not recommended'} | "
        f"{'; '.join(q.get('reasons', [])[:3]) or '—'} |"
        for n, q in qa_by_name.items()
    ]
    return "\n".join([
        f"# {req['prompt']}",
        f"**Job:** `{job.id}`  \n**State:** `{job.state}`  \n**Selected:** {selected or '—'}",
        "", "| Candidate | QA | Reasons |", "|---|---|---|", *rows, "",
        "QA is advisory only; any candidate can be approved.", "",
        "**Final prompt:**", "", final.read_text() if final.is_file() else "—",
    ])


class LineAReviewGallery(_SideEffectNode):
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("job_id",)
    FUNCTION = "run"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {"required": {"job_id": ("STRING", {"default": "latest", "tooltip": "'latest' = newest job awaiting review"})}}

    def run(self, job_id: str) -> dict:
        job = resolve_job(job_id)
        sel_file = job.path("selected/selected_candidate.txt")
        selected = sel_file.read_text().strip() if sel_file.is_file() else None
        images, qa_by_name, tiles = [], {}, []
        for path in sorted(job.path("candidates").glob("*.png")):
            stem = path.stem
            qa_path = job.path(f"qa/{stem}.json")
            qa = job.read_json(f"qa/{stem}.json") if qa_path.is_file() else {}
            qa_by_name[path.name] = qa
            tile = render_tile(path, stem, qa, selected == stem)
            tiles.append(tile)
            dst, item = temp_target(job, f"review_{stem}.png")
            tile.save(dst)
            images.append(item)
        if len(tiles) > 1:  # overview first: all variants side by side for quick comparison
            dst, item = temp_target(job, "review_overview.png")
            overview(tiles).save(dst)
            images.insert(0, item)
        md, report = temp_target(job, "summary.md")
        md.write_text(summary_markdown(job, qa_by_name, selected))
        return {"ui": {"images": images, "reports": [report]}, "result": (job.id,)}


class LineAApprove(_SideEffectNode):
    """Human approval gate: 'review only' blocks all downstream 3D nodes."""
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("job_id",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {
            "required": {
                "job_id": ("STRING", {"forceInput": True}),
                "approve_candidate": (CANDIDATE_CHOICES, {"default": REVIEW_ONLY}),
            }
        }

    def run(self, job_id: str, approve_candidate: str) -> tuple:
        if approve_candidate == REVIEW_ONLY:
            return (ExecutionBlocker(None),)
        select_candidate(load_job(job_id), int(approve_candidate))
        return (job_id,)
