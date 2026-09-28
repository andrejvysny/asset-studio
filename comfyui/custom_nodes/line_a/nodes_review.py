"""App Mode review/approval: QA-annotated gallery + explicit approve step that gates the 3D stages."""
from __future__ import annotations

import textwrap
from pathlib import Path

from comfy_execution.graph_utils import ExecutionBlocker
from PIL import Image, ImageDraw, ImageFont

from .jobcore.approval import approve, read_candidate_set
from .jobcore.job_io import SELECTABLE, Job, JobError
from .nodes_job import _SideEffectNode, load_job
from .settings import OUTPUT_ROOT
from .ui_files import temp_target

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


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow without FreeType sizes
        return ImageFont.load_default()


def render_tile(image_path: Path, label: str, qa: dict | None, selected: bool) -> Image.Image:
    img = Image.open(image_path).convert("RGB").resize((TILE, TILE))
    status = (qa or {}).get("status")
    color = GREEN if status == "recommended" else ORANGE if status == "not_recommended" else GRAY
    badge = {"recommended": "RECOMMENDED", "not_recommended": "NOT RECOMMENDED",
             "unverified": "UNVERIFIED"}.get(status or "", "NO QA")
    cov = (qa or {}).get("coverage")
    if cov:
        badge += f"  {cov['ran']}/{cov['total']}"
    reasons = ((qa or {}).get("reasons", []) + (qa or {}).get("warnings", []))[:4]
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
    label = {"recommended": "✅ recommended", "not_recommended": "⚠️ not recommended", "unverified": "❔ unverified"}
    rows = [
        f"| #{n.removesuffix('.png')} | {label.get(q.get('status'), 'no QA')} "
        f"({(q.get('coverage') or {}).get('ran', 0)}/{(q.get('coverage') or {}).get('total', 0)}) | "
        f"{'; '.join((q.get('reasons', []) + q.get('warnings', []))[:3]) or '—'} |"
        for n, q in qa_by_name.items()
    ]
    set_id = job.read_json("candidates/set.json")["set_id"] if job.path("candidates/set.json").is_file() else "—"
    return "\n".join([
        f"# {req['prompt']}",
        f"**Job id (copy into Approve):** `{job.id}`  \n**Candidate set:** `{set_id}`  \n"
        f"**State:** `{job.state}`  \n**Current attempt uses:** {selected or '—'}",
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
        current = job.read_json("job_state.json").get("current_attempt")
        selected = f"{job.read_attempt(current)['index']:02d}" if current else None
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
    """Human approval gate. 'review only' blocks all downstream 3D nodes. Approval needs the concrete job id
    shown in the gallery ('latest' is refused), so a newer job can never receive an approval."""
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("job_id", "attempt_id")
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {
            "required": {
                "job_id": ("STRING", {"default": "", "tooltip": "exact job id from the gallery summary"}),
                "approve_candidate": (CANDIDATE_CHOICES, {"default": REVIEW_ONLY}),
                "override_qa": ("BOOLEAN", {"default": False,
                                            "tooltip": "approve even if the candidate is not QA-recommended"}),
            }
        }

    def run(self, job_id: str, approve_candidate: str, override_qa: bool) -> tuple:
        if approve_candidate == REVIEW_ONLY:
            return (ExecutionBlocker(None), ExecutionBlocker(None))
        if job_id.strip() in ("", "latest"):
            raise JobError("enter the exact job id shown in the gallery summary to approve")
        job = load_job(job_id)
        cset = read_candidate_set(job)
        attempt, _ = approve(job, cset["set_id"], int(approve_candidate), cset["images"][approve_candidate],
                             override=override_qa)
        return (job.id, attempt["id"])
