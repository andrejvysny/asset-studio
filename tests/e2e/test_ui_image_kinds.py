"""UI for Phase 5 kinds: sprite build view, frame-sequence import + player, material bundle mapping. SIMULATED engine."""
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
NAME = "E2E kinds"


def _kinds_project(studio_url: str) -> str:
    for p in httpx.get(f"{studio_url}/api/v1/projects").json()["projects"]:
        if p["name"] == NAME:
            return p["id"]
    pid = httpx.post(f"{studio_url}/api/v1/projects", json={"name": NAME}, headers=H).json()["id"]
    cfg = httpx.get(f"{studio_url}/api/v1/projects/{pid}/config").json()["config"]
    cfg["categories"] = [{"id": "sprites", "slug": "sprites", "label": "Sprites", "defaults": {"kind": "sprite"}}]
    r = httpx.patch(f"{studio_url}/api/v1/projects/{pid}/config", json={"expected_revision": 1, "config": cfg}, headers=H)
    assert r.status_code == 200, r.text
    return pid


def _png(path: Path, w: int, h: int, color: tuple[int, int, int, int]) -> Path:
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), color).save(buf, "PNG")
    path.write_bytes(buf.getvalue())
    return path


def test_sprite_build_shows_cutout_and_pivot(page, studio_url: str) -> None:
    pid = _kinds_project(studio_url)
    page.goto(f"/p/{pid}/jobs/new?cat=sprites")
    page.locator("label.field", has_text="Name").locator("input").fill("Brass lantern")
    page.get_by_label("Brief").fill("small brass lantern")
    page.get_by_role("button", name=re.compile(r"^Save and run$")).click()
    expect(page.get_by_label("prompt", exact=True)).to_have_value(re.compile("simulated enhancement"), timeout=15000)
    page.get_by_role("button", name=re.compile(r"^Confirm prompt \+ generate \d* ?candidates$")).click()
    expect(page.get_by_role("button", name=re.compile(r"^candidate 1 of round 1"))).to_be_visible(timeout=20000)
    expect(page.get_by_text("QA pending")).to_have_count(0, timeout=20000)
    page.keyboard.press("1")
    if page.get_by_role("dialog").is_visible():
        page.get_by_role("button", name="Approve anyway").click()
    expect(page.get_by_role("button", name="Approved ✓ · click to undo")).to_be_visible(timeout=10000)
    page.get_by_role("button", name=re.compile(r"^Cut out →")).click()
    expect(page).to_have_url(re.compile(r"/build$"))
    expect(page.get_by_role("button", name="Accept attempt 1", exact=True)).to_be_visible(timeout=15000)
    expect(page.get_by_label("pivot")).to_be_visible()
    expect(page.get_by_text("sprite_has_transparency")).to_be_visible()
    shot(page, "k1-sprite-build")
    assert page.external == [], page.external  # type: ignore[attr-defined]


def test_frame_sequence_import_plays(page, studio_url: str, tmp_path: Path) -> None:
    pid = _kinds_project(studio_url)
    frames = [_png(tmp_path / f"spark_{i}.png", 16, 16, (40 * i, 200, 80, 255)) for i in range(1, 5)]
    page.goto(f"/p/{pid}/assets")
    page.get_by_role("button", name="Import files").first.click()
    page.get_by_role("tab", name="Frame sequence").click()
    page.get_by_label("file").set_input_files([str(f) for f in frames])
    expect(page.get_by_text(re.compile(r"4 frames · 16×16 px"))).to_be_visible(timeout=10000)
    page.locator("label.field", has_text="Kind").locator("select").select_option("vfx_flipbook")
    page.locator("label.field", has_text="Name").locator("input").fill("Spark burst")
    shot(page, "k2-frames-import")
    page.get_by_role("button", name="Publish as v1 (imported)").click()
    expect(page.get_by_label("frame player")).to_be_visible(timeout=10000)
    expect(page.get_by_text(re.compile(r"/4 · 12 fps"))).to_be_visible()
    shot(page, "k3-flipbook-detail")
    assert page.external == [], page.external  # type: ignore[attr-defined]


def test_material_bundle_mapping(page, studio_url: str, tmp_path: Path) -> None:
    pid = _kinds_project(studio_url)
    files = [_png(tmp_path / "stone_albedo.png", 32, 32, (120, 110, 100, 255)),
             _png(tmp_path / "stone_mystery.png", 32, 32, (128, 128, 255, 255))]
    page.goto(f"/p/{pid}/assets")
    page.get_by_role("button", name="Import files").first.click()
    page.get_by_role("tab", name="Material bundle").click()
    page.get_by_label("file").set_input_files([str(f) for f in files])
    publish = page.get_by_role("button", name="Publish as v1 (imported)")
    expect(publish).to_be_disabled(timeout=10000)  # an unmapped file blocks commit
    page.get_by_label("role for stone_mystery.png").select_option("normal")
    page.locator("label.field", has_text="Name").locator("input").fill("Stone")
    expect(publish).to_be_enabled()
    shot(page, "k4-material-mapping")
    publish.click()
    expect(page.get_by_label("tiled preview")).to_be_visible(timeout=10000)
    expect(page.get_by_role("button", name="normal", exact=True)).to_be_visible()
    assert page.external == [], page.external  # type: ignore[attr-defined]
