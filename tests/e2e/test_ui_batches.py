"""Jobs + Batch flow in the browser with the SIMULATED engine: save Jobs (no inference) -> New Batch + picker ->
Start -> Review: confirm prompts across Jobs -> approve -> build/accept -> Execution tab. Not GPU evidence."""
from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import shot

pytestmark = pytest.mark.e2e
H = {"x-assetstudio": "1"}


def _project(url: str) -> str:
    pid = httpx.post(f"{url}/api/v1/projects", json={"name": "Batch UI"}, headers=H).json()["id"]
    cfg = httpx.get(f"{url}/api/v1/projects/{pid}/config").json()["config"]
    cfg["categories"] = [{"id": "concept", "slug": "concept", "label": "Concept", "defaults": {"kind": "concept_art"}},
                         {"id": "icons", "slug": "icons", "label": "Icons", "defaults": {"kind": "icon"}}]
    httpx.patch(f"{url}/api/v1/projects/{pid}/config", json={"expected_revision": 1, "config": cfg}, headers=H)
    return pid


def test_four_jobs_one_batch_cross_job_waves(page, studio_url: str) -> None:
    pid = _project(studio_url)
    names = ["Tavern", "Harbour", "Sword", "Mill"]
    for name, cat in zip(names, ["concept", "concept", "icons", "concept"], strict=True):
        page.goto(f"/p/{pid}/jobs/new?cat={cat}")
        page.get_by_label("Brief").fill(f"a {name.lower()}")
        page.locator("label.field", has_text="Name").locator("input").fill(name)
        page.get_by_role("button", name="Save Job", exact=True).click()
        expect(page).to_have_url(re.compile(r"/jobs/job_[a-z0-9]+"))
    tasks = httpx.get(f"{studio_url}/api/v2/tasks", params={"project_id": pid}).json()["tasks"]
    assert tasks == []  # JB01: saving Jobs never starts inference
    page.goto(f"/p/{pid}/batches")
    page.get_by_role("button", name="New Batch").click()
    expect(page).to_have_url(re.compile(r"/batches/bch_[a-z0-9]+\?add=1"))
    expect(page.get_by_text("No Jobs yet. Add saved Jobs from this project.")).to_be_visible()
    for name in names:
        page.get_by_label(f"add {name}").click()
    page.get_by_role("button", name="Add 4 Jobs").click()
    expect(page.get_by_role("table", name="Jobs in this Batch")).to_contain_text("Mill")
    expect(page.get_by_text(re.compile(r"Start enhances 4 prompts in one pass on GPU1"))).to_be_visible()
    shot(page, "batch-01-overview")
    page.get_by_role("button", name="Start Batch · enhance 4 prompts").click()
    expect(page).to_have_url(re.compile(r"/batches/bch_[a-z0-9]+/execution"))
    page.get_by_role("tab", name=re.compile("Review")).click()
    expect(page.get_by_role("region", name="Prompts to confirm")).to_be_visible(timeout=30000)
    page.get_by_role("button", name="Confirm 4 selected + generate").click()
    cands = page.get_by_role("region", name="Candidates to approve")
    expect(cands).to_be_visible(timeout=30000)
    shot(page, "batch-02-candidates")
    page.get_by_role("button", name="Approve best recommended in 4").click()
    for _ in range(4):  # whatever QA did not recommend needs an explicit override
        page.wait_for_timeout(1500)
        if not cands.is_visible():
            break
        cands.get_by_role("button", name=re.compile(r"^approve .* candidate 1")).first.click()
        dialog = page.get_by_role("dialog")
        if dialog.is_visible():
            dialog.get_by_role("button", name="Approve anyway").click()
    expect(page.get_by_role("button", name="Build 4 approved")).to_be_visible(timeout=20000)
    page.get_by_role("button", name="Build 4 approved").click()
    expect(page.get_by_role("button", name="Accept 4 built")).to_be_enabled(timeout=30000)
    page.get_by_role("button", name="Accept 4 built").click()
    expect(page.get_by_role("button", name="Publish 4 accepted")).to_be_visible(timeout=20000)
    shot(page, "batch-03-publish")
    page.get_by_role("tab", name=re.compile("Execution")).click()
    expect(page.get_by_text("GPU0 · image")).to_be_visible()
    expect(page.get_by_text("GPU1 · text, QA, 3D")).to_be_visible()
    expect(page.get_by_text("Recent passes")).to_be_visible(timeout=20000)
    shot(page, "batch-04-execution")
    passes = httpx.get(f"{studio_url}/api/v2/passes").json()["passes"]
    enhance = [p for p in passes if p["residency"].startswith("aux.vlm") and len(p["task_ids"]) == 4]
    assert enhance and len(enhance[0]["jobs"]) == 4  # one VLM residency served all four Jobs
    assert not [e for e in page.errors if "favicon" not in e], page.errors  # type: ignore[attr-defined]
    assert page.external == [], f"offline violation: {page.external}"  # type: ignore[attr-defined]
