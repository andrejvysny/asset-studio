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
    page.goto(f"/p/{pid}/batches/new?cat=concept")
    expect(page.get_by_role("heading", name="What are you making?")).to_be_visible()
    page.get_by_label("briefs").fill("Tavern interior: warm tavern, long tables\nHarbour at dusk: small fishing harbour")
    page.locator("label.field", has_text="Name").locator("input").fill("Concept round 1")
    shot(page, "01-new-batch")
    page.get_by_role("button", name=re.compile(r"Create batch \+ enhance 2 prompts")).click()
    expect(page).to_have_url(re.compile(r"/batches/(bat|job)_[a-z0-9]+"))
    expect(page.get_by_label("prompt Tavern interior")).to_have_value(re.compile("simulated enhancement"), timeout=15000)
    shot(page, "02-prompts")
    page.get_by_role("button", name=re.compile(r"Confirm 2 prompts \+ generate")).click()
    expect(page).to_have_url(re.compile(r"/candidates$"))
    expect(page.get_by_text("2/2 items have candidates.")).to_be_visible(timeout=20000)
    shot(page, "03-candidates")
    page.get_by_role("link", name=re.compile("Approve")).first.click()
    expect(page.get_by_label("candidate inspector")).to_be_visible(timeout=10000)
    page.keyboard.press("2")  # approve B of the first row (override dialog if not recommended)
    if page.get_by_role("dialog").is_visible():
        page.get_by_role("button", name="Approve anyway").click()
    expect(page.get_by_text(re.compile(r"^1 approved"))).to_be_visible(timeout=10000)
    shot(page, "04-approve")
    page.get_by_role("button", name=re.compile(r"for 1 approved")).click()
    expect(page).to_have_url(re.compile(r"/build$"))
    page.get_by_role("button", name="Accept", exact=True).click(timeout=15000)
    expect(page.get_by_role("button", name="Accepted ✓")).to_be_visible(timeout=10000)
    shot(page, "05-build")
    page.get_by_role("button", name="Go to publish →").click()
    expect(page.get_by_text("concept_tavern_interior")).to_be_visible(timeout=10000)
    page.get_by_role("button", name="Publish 1 versions").click()
    expect(page.get_by_text("v1 published")).to_be_visible(timeout=15000)
    shot(page, "06-publish")
    page.goto(f"/p/{pid}/assets")
    expect(page.get_by_text("1 asset · 0 planned")).to_be_visible(timeout=10000)
    page.get_by_role("link", name=re.compile("Tavern interior")).click()
    expect(page.get_by_text("licence: not_cleared")).to_be_visible()  # simulated output never reads as cleared
    shot(page, "07-asset-detail")
    assert not [e for e in page.errors if "favicon" not in e], page.errors  # type: ignore[attr-defined]
    assert page.external == [], f"offline violation: {page.external}"  # type: ignore[attr-defined]


@pytest.mark.parametrize("screen,heading", [
    ("assets", "All assets"), ("shots", "Shot list"), ("batches", "Batches"), ("schema", "Concept"),
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
    page.goto(f"/p/{pid}/batches/new?cat=containers")
    page.get_by_label("briefs").fill("Supply crate: small wooden supply crate")
    page.locator("label.field", has_text="Name").locator("input").fill("Props round 1")
    page.get_by_role("button", name=re.compile(r"Create batch \+ enhance 1 prompts")).click()
    expect(page.get_by_label("prompt Supply crate")).to_have_value(re.compile("simulated enhancement"), timeout=15000)
    page.get_by_role("button", name=re.compile(r"Confirm 1 prompts \+ generate")).click()
    expect(page.get_by_text("1/1 items have candidates.")).to_be_visible(timeout=20000)
    page.get_by_role("link", name=re.compile("Approve")).first.click()
    expect(page.get_by_label("candidate inspector")).to_be_visible(timeout=10000)
    page.keyboard.press("1")
    if page.get_by_role("dialog").is_visible():
        page.get_by_role("button", name="Approve anyway").click()
    page.get_by_role("button", name=re.compile(r"for 1 approved")).click()
    expect(page.get_by_role("button", name="Accept", exact=True)).to_be_visible(timeout=15000)
    expect(page.get_by_text("triangle_budget")).to_be_visible()
    expect(page.locator("model-viewer")).to_be_attached(timeout=15000)
    shot(page, "08-3d-build")
    page.get_by_role("button", name="Re-export…").click()
    page.locator("label.field", has_text="Texture").locator("select").select_option("1024")
    page.get_by_role("button", name="Re-export", exact=True).click()
    expect(page.get_by_role("dialog")).to_be_hidden(timeout=10000)
    expect(page.get_by_role("button", name="Accept", exact=True)).to_be_visible(timeout=15000)
    shot(page, "09-3d-reexport")
    assert not [e for e in page.errors if "favicon" not in e], page.errors  # type: ignore[attr-defined]
    assert page.external == [], f"offline violation: {page.external}"  # type: ignore[attr-defined]
