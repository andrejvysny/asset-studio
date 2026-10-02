"""Create a Job from an image (no prompt, no previews) in the UI. SIMULATED engine."""
from __future__ import annotations

import io
import re
import uuid
from pathlib import Path

import httpx
import pytest
from PIL import Image
from playwright.sync_api import expect

from tests.e2e.conftest import shot

pytestmark = pytest.mark.e2e
H = {"x-assetstudio": "1"}


def _project(studio_url: str) -> str:
    base = f"{studio_url}/api/v1/projects"
    pid = httpx.post(base, json={"name": f"E2E {uuid.uuid4().hex[:6]}"}, headers=H).json()["id"]
    cfg = httpx.get(f"{base}/{pid}/config").json()
    c = cfg["config"]
    c["categories"] = [{"id": "props", "slug": "props", "label": "Props", "defaults": {"kind": "model3d"}}]
    httpx.patch(f"{base}/{pid}/config", json={"expected_revision": cfg["revision"], "config": c}, headers=H).raise_for_status()
    return pid


def test_from_image_job_goes_straight_to_candidates(page, studio_url: str, tmp_path: Path) -> None:
    pid = _project(studio_url)
    buf = io.BytesIO()
    Image.new("RGB", (96, 96), (180, 90, 40)).save(buf, "PNG")
    src = tmp_path / "crate.png"
    src.write_bytes(buf.getvalue())

    page.goto(f"/p/{pid}/jobs/new?cat=props")
    page.locator("label.field", has_text="Name").locator("input").fill("Supply crate")
    page.get_by_role("group", name="generation mode").get_by_role("button", name="From image").click()
    expect(page.get_by_role("button", name="Prompt + image")).to_be_disabled()
    expect(page.get_by_role("button", name="Save and run")).to_be_disabled()  # a source image is required
    page.get_by_label("upload reference image").set_input_files(str(src))
    expect(page.get_by_text("Source image · required")).to_be_visible()
    expect(page.get_by_role("button", name="Save and run")).to_be_enabled(timeout=10000)
    shot(page, "image_mode_form")
    page.get_by_role("button", name="Save and run").click()

    expect(page).to_have_url(re.compile(r"/jobs/job_[a-z0-9]+"))
    expect(page.get_by_text("from image", exact=True)).to_be_visible(timeout=10000)
    expect(page.get_by_alt_text("source image")).to_be_visible()
    expect(page.get_by_role("button", name=re.compile(r"^candidate 1 of round 1"))).to_be_visible(timeout=20000)
    expect(page.get_by_text("Source image used as given")).to_be_visible()
    shot(page, "image_mode_job")
