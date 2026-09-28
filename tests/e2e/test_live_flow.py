"""Asset Studio against the running stack (:8190). GPU flow is opt-in: E2E_GPU=1 (enhance ~20 s, 4 Lightning
candidates + QA ~90 s, then 3D until the current runtime blocker)."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect

if TYPE_CHECKING:
    from conftest import StudioPage

ROOT = Path(__file__).resolve().parents[2]


def _jobs(base: str) -> list[dict]:
    with urllib.request.urlopen(base + "/api/jobs") as r:
        return json.loads(r.read())


def test_live_screens_render(live_page: StudioPage, live_base: str) -> None:
    for path, text in [("/library/forest", "Temperate forest"), ("/coverage", "Catalog coverage"), ("/jobs", "Jobs"),
                       ("/review", "Review"), ("/runtime", "Models · config/models.yaml vs installed")]:
        page = live_page.goto(path)
        expect(page.get_by_text(text, exact=False).first).to_be_visible()
        live_page.shot(path.strip("/").replace("/", "_"))
    # runtime shows real model rows and worker status
    expect(page.get_by_text("qwen_image_2512", exact=True)).to_be_visible()
    for job in _jobs(live_base)[:6]:  # every job's primary screen opens without errors
        live_page.goto(f"/review/{job['job_id']}", settle_ms=800)
        if job["current_attempt"]:
            live_page.goto(f"/attempts/{job['job_id']}", settle_ms=800)


@pytest.mark.skipif(os.environ.get("E2E_GPU") != "1", reason="GPU flow is opt-in: E2E_GPU=1")
def test_full_job_flow_with_qa_override(live_page: StudioPage) -> None:
    page = live_page.goto("/new")
    page.locator("textarea").fill("small wooden bucket with rope handle")
    page.locator("select").first.select_option("small_prop")
    live_page.shot("01_brief")
    page.get_by_role("button", name="Create job + enhance prompt").click()
    page.wait_for_url(re.compile(r"/new/\d{8}-"), timeout=300_000)
    job = page.url.rsplit("/", 1)[1]
    expect(page.get_by_text("Enhanced prompt (editable)")).to_be_visible(timeout=60_000)
    assert not (ROOT / "output" / job / "candidates").exists(), "images generated before prompt confirmation"
    live_page.shot("02_enhanced")

    ta = page.locator("textarea")
    ta.fill(ta.input_value() + " Iron bands, no lid.")
    expect(page.get_by_text("Edited · reset")).to_be_visible()
    page.locator("select").select_option("lightning_8step")
    page.get_by_role("button", name=re.compile("Confirm prompt")).click()
    page.wait_for_url(re.compile(f"/review/{job}"), timeout=60_000)
    approve = page.get_by_role("button", name=re.compile(r"^Approve #"))
    running = page.get_by_role("button", name=re.compile("running…$"))
    expect(running).to_be_visible(timeout=120_000)  # generation/QA in progress: not approvable, no override offered
    expect(page.get_by_label("Override QA")).to_have_count(0)
    expect(approve).to_be_visible(timeout=600_000)
    live_page.shot("03_review_focus")
    edited = (ROOT / "output" / job / "enhanced-prompt.final.txt").read_text()
    assert "Iron bands, no lid" in edited and edited.count("single isolated object") == 1
    assert edited.rstrip().endswith("stylized hand-painted game asset")

    for view in ("Grid", "Check matrix", "Focus"):
        page.get_by_role("button", name=re.compile(view)).click()
        page.wait_for_timeout(500)
        live_page.shot(f"04_view_{view.split()[0].lower()}")

    # pick the first non-recommended candidate if any: its approval must require the explicit override
    thumbs = page.locator("div.clickable:has(img[alt^='candidate'])")
    qa = json.loads(urllib.request.urlopen(f"{live_page.base}/api/jobs/{job}").read())["qa"]
    bad = [k for k, v in qa.items() if v["status"] != "recommended"]
    if bad:
        thumbs.nth(int(bad[0])).click()
        expect(approve).to_be_disabled()
        expect(approve).to_have_text(re.compile("anyway"))
        page.get_by_label("Override QA").check()
        live_page.shot("05_override_ticked")
    expect(approve).to_be_enabled()
    approve.click()
    page.wait_for_url(re.compile(f"/attempts/{job}"), timeout=60_000)
    expect(page.get_by_text("att-01").first).to_be_visible()
    attempt = json.loads((ROOT / "output" / job / "model" / "attempts" / "att-01" / "attempt.json").read_text())
    assert attempt["qa_override"] == bool(bad)
    att_file = ROOT / "output" / job / "model" / "attempts" / "att-01" / "attempt.json"
    deadline = time.time() + 900
    while json.loads(att_file.read_text())["state"] in {"approved", "cutout_completed"} and time.time() < deadline:
        time.sleep(5)
    final = json.loads(att_file.read_text())
    assert final["state"] in {"completed", "failed_trellis", "failed_postprocess"}, final["state"]
    page.wait_for_timeout(5000)  # page polls every 4 s
    expect(page.get_by_text(final["state"], exact=True).first).to_be_visible()
    live_page.shot("06_attempt")
