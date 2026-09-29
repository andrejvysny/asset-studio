"""UI walk-through of every screen + the full concept-art lifecycle, SIMULATED engine (U01, B01, P01)."""
from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import shot

pytestmark = pytest.mark.e2e
H = {"x-assetstudio": "1"}


def _project(studio_url: str) -> str:
    projects = httpx.get(f"{studio_url}/api/v1/projects").json()["projects"]
    if projects:
        return projects[0]["id"]
    pid = httpx.post(f"{studio_url}/api/v1/projects", json={"name": "E2E demo"}, headers=H).json()["id"]
    cfg = httpx.get(f"{studio_url}/api/v1/projects/{pid}/config").json()["config"]
    cfg["categories"] = [
        {"id": "concept", "slug": "concept", "label": "Concept", "defaults": {"kind": "concept_art", "naming": "concept_{name}"}},
        {"id": "props", "slug": "props", "label": "Props", "defaults": {"kind": "model3d"}},
        {"id": "containers", "parent_id": "props", "slug": "containers", "label": "Containers",
         "defaults": {"budget": {"triangles": {"min": 300, "max": 1200}}}}]
    r = httpx.patch(f"{studio_url}/api/v1/projects/{pid}/config", json={"expected_revision": 1, "config": cfg}, headers=H)
    assert r.status_code == 200, r.text
    return pid


def test_onboarding_empty_state(page, studio_url: str) -> None:
    if httpx.get(f"{studio_url}/api/v1/projects").json()["projects"]:
        pytest.skip("project already exists in this session")
    page.goto("/")
    expect(page.get_by_role("heading", name="Create a project")).to_be_visible()
    shot(page, "00-onboarding")


def test_full_concept_lifecycle(page, studio_url: str) -> None:
    pid = _project(studio_url)
    page.goto(f"/p/{pid}/jobs/new?cat=concept")
    expect(page.get_by_role("heading", name="What are you making?")).to_be_visible()
    page.locator("label.field", has_text="Name").locator("input").fill("Tavern interior")
    page.get_by_label("Brief").fill("warm tavern, long tables")
    shot(page, "01-new-job")
    page.get_by_role("button", name=re.compile(r"^Save and run$")).click()
    expect(page).to_have_url(re.compile(r"/jobs/job_[a-z0-9]+"))
    expect(page.get_by_label("prompt", exact=True)).to_have_value(re.compile("simulated enhancement"), timeout=15000)
    shot(page, "02-prompts")
    page.get_by_role("button", name=re.compile(r"^Confirm prompt \+ generate \d* ?candidates$")).click()
    expect(page.get_by_role("button", name=re.compile(r"^candidate 2 of round 1"))).to_be_visible(timeout=20000)
    expect(page.get_by_text("QA pending")).to_have_count(0, timeout=20000)
    shot(page, "03-candidates")
    page.keyboard.press("2")  # approve #2 (override dialog if QA does not recommend it)
    if page.get_by_role("dialog").is_visible():
        page.get_by_role("button", name="Approve anyway").click()
    expect(page.get_by_role("button", name="Approved ✓ · click to undo")).to_be_visible(timeout=10000)
    shot(page, "04-approve")
    page.get_by_role("button", name=re.compile(r"^Upscale →")).click()
    expect(page).to_have_url(re.compile(r"/build$"))
    page.get_by_role("button", name="Accept attempt 1", exact=True).click(timeout=15000)
    expect(page.get_by_role("button", name=re.compile(r"^Accepted attempt 1"))).to_be_visible(timeout=10000)
    shot(page, "05-build")
    page.get_by_role("link", name=re.compile(r"Publish$")).click()
    expect(page.get_by_text("concept_tavern_interior")).to_be_visible(timeout=10000)
    page.get_by_role("button", name="Publish", exact=True).click()
    expect(page.get_by_role("link", name="v1 published")).to_be_visible(timeout=15000)
    shot(page, "06-publish")
    page.goto(f"/p/{pid}/assets")
    expect(page.get_by_text("1 asset · 0 planned")).to_be_visible(timeout=10000)
    page.get_by_role("link", name=re.compile("Tavern interior")).click()
    expect(page.get_by_text("licence: not_cleared")).to_be_visible()  # simulated output never reads as cleared
    shot(page, "07-asset-detail")
    assert not [e for e in page.errors if "favicon" not in e], page.errors  # type: ignore[attr-defined]
    assert page.external == [], f"offline violation: {page.external}"  # type: ignore[attr-defined]


@pytest.mark.parametrize("screen,heading", [
    ("assets", "All assets"), ("shots", "Shot list"), ("jobs", "Jobs"), ("batches", "Batches"), ("schema", "Concept"),
    ("pipelines", "3D model"), ("qa", "3D model candidates checks"), ("style", "Style"), ("storage", "Storage"),
    ("export", "Export targets"), ("runtime", "Runtime"),
])
def test_every_screen_renders(page, studio_url: str, screen: str, heading: str) -> None:
    pid = _project(studio_url)
    page.goto(f"/p/{pid}/{screen}")
    expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible(timeout=10000)
    expect(page.get_by_text("SIMULATED ENGINE", exact=True)).to_be_visible()
    shot(page, f"screen-{screen}")
    assert not [e for e in page.errors if "favicon" not in e], page.errors  # type: ignore[attr-defined]


def test_3d_build_and_reexport(page, studio_url: str) -> None:
    pid = _project(studio_url)
    page.goto(f"/p/{pid}/jobs/new?cat=containers")
    page.locator("label.field", has_text="Name").locator("input").fill("Supply crate")
    page.get_by_label("Brief").fill("small wooden supply crate")
    page.get_by_role("button", name=re.compile(r"^Save and run$")).click()
    expect(page.get_by_label("prompt", exact=True)).to_have_value(re.compile("simulated enhancement"), timeout=15000)
    page.get_by_role("button", name=re.compile(r"^Confirm prompt \+ generate \d* ?candidates$")).click()
    expect(page.get_by_role("button", name=re.compile(r"^candidate 1 of round 1"))).to_be_visible(timeout=20000)
    expect(page.get_by_text("QA pending")).to_have_count(0, timeout=20000)
    page.keyboard.press("1")
    if page.get_by_role("dialog").is_visible():
        page.get_by_role("button", name="Approve anyway").click()
    expect(page.get_by_role("button", name="Approved ✓ · click to undo")).to_be_visible(timeout=10000)
    page.get_by_role("button", name=re.compile(r"^Generate 3D →")).click()
    expect(page.get_by_role("button", name="Accept attempt 1", exact=True)).to_be_visible(timeout=15000)
    expect(page.get_by_text("triangle_budget")).to_be_visible()
    expect(page.locator("model-viewer")).to_be_attached(timeout=15000)
    shot(page, "08-3d-build")
    page.get_by_role("button", name=re.compile(r"^Re-export from raw")).click()
    page.locator("label.field", has_text="Texture").locator("select").select_option("1024")
    page.get_by_role("button", name="Re-export", exact=True).click()
    expect(page.get_by_role("dialog")).to_be_hidden(timeout=10000)
    expect(page.get_by_text("Attempt 2 · re-export from raw")).to_be_visible(timeout=15000)
    expect(page.get_by_role("button", name="Accept attempt 2", exact=True)).to_be_visible(timeout=15000)
    shot(page, "09-3d-reexport")
    assert not [e for e in page.errors if "favicon" not in e], page.errors  # type: ignore[attr-defined]
    assert page.external == [], f"offline violation: {page.external}"  # type: ignore[attr-defined]
