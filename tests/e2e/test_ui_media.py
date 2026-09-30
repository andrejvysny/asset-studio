"""Media Library UI: upload, preview + tag edit, New Job from media, archive. SIMULATED engine."""
from __future__ import annotations

import io
import re
from pathlib import Path

import httpx
import pytest
from PIL import Image
from playwright.sync_api import expect

from tests.e2e.conftest import shot

pytestmark = pytest.mark.e2e
H = {"x-assetstudio": "1"}


def _project(studio_url: str) -> str:
    return httpx.post(f"{studio_url}/api/v1/projects", json={"name": "E2E media"}, headers=H).json()["id"]


def _png(path: Path, color: tuple[int, int, int]) -> Path:
    buf = io.BytesIO()
    Image.new("RGB", (96, 64), color).save(buf, "PNG")
    path.write_bytes(buf.getvalue())
    return path


def test_upload_tag_new_job_and_archive(page, studio_url: str, tmp_path: Path) -> None:
    pid = _project(studio_url)
    page.goto(f"/p/{pid}/media")
    files = [_png(tmp_path / "moodboard.png", (200, 80, 40)), _png(tmp_path / "palette.png", (40, 80, 200))]
    page.get_by_label("upload media").set_input_files([str(f) for f in files])
    expect(page.get_by_role("button", name="open moodboard")).to_be_visible(timeout=10000)
    expect(page.get_by_role("button", name="open palette")).to_be_visible()
    expect(page.get_by_role("link", name=re.compile(r"^Media\s*2$"))).to_be_visible(timeout=10000)

    # Same bytes again: reported as duplicate, not added twice.
    page.get_by_label("upload media").set_input_files(str(files[0]))
    expect(page.get_by_text(re.compile(r"1 duplicate"))).to_be_visible(timeout=10000)
    expect(page.get_by_role("button", name="open moodboard")).to_have_count(1)

    page.get_by_role("button", name="open moodboard").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label(re.compile("tags", re.I)).fill("Warm, Rust")
    dialog.get_by_role("button", name="Save").click()
    expect(dialog.get_by_role("button", name="Save")).to_be_disabled(timeout=10000)
    shot(page, "media_preview")
    dialog.get_by_role("button", name="New Job from this").click()

    expect(page).to_have_url(re.compile(r"/jobs/new\?media=med_"))
    expect(page.get_by_label("note for moodboard")).to_be_visible(timeout=10000)
    expect(page.get_by_role("link", name=re.compile(r"^Media\s*2$"))).to_be_visible(timeout=10000)
    shot(page, "media_new_job_ref")

    page.goto(f"/p/{pid}/media")
    expect(page.get_by_role("button", name="warm")).to_be_visible(timeout=10000)
    page.get_by_role("button", name="open palette").click()
    page.get_by_role("dialog").get_by_role("button", name="Archive").click()
    page.keyboard.press("Escape")
    expect(page.get_by_role("button", name="open palette")).to_have_count(0, timeout=10000)
    page.get_by_label("show archived").check()
    expect(page.get_by_role("button", name="open palette")).to_be_visible(timeout=10000)
    assert not page.errors, page.errors  # type: ignore[attr-defined]
