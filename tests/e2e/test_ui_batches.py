"""Jobs + Batch flow in the browser with the SIMULATED engine: save Jobs (no inference) -> group -> plan ->
start -> one cross-Job confirmation wave -> candidates -> approval wave -> build wave. Not GPU evidence."""
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


def test_three_jobs_one_batch_cross_job_waves(page, studio_url: str) -> None:
    pid = _project(studio_url)
    for n, (cat, names) in enumerate([("concept", ["Tavern", "Harbour"]), ("icons", ["Sword"]), ("concept", ["Mill"])]):
        page.goto(f"/p/{pid}/jobs/new?cat={cat}")
        page.get_by_label("briefs").fill("\n".join(f"{x}: a {x.lower()}" for x in names))
        page.locator("label.field", has_text="Name").locator("input").fill(f"Job {n + 1}")
        page.get_by_role("button", name=re.compile(r"^Save Job")).click()
        expect(page).to_have_url(re.compile(r"/jobs/job_[a-z0-9]+"))
    tasks = httpx.get(f"{studio_url}/api/v2/tasks", params={"project_id": pid}).json()["tasks"]
    assert tasks == []  # JB01: saving Jobs never starts inference
    page.goto(f"/p/{pid}/jobs")
    for n in (1, 2, 3):
        page.get_by_label(f"select Job {n}").click()
    page.get_by_role("button", name="Create Batch").click()
    expect(page).to_have_url(re.compile(r"/batches/bch_[a-z0-9]+"))
    page.get_by_role("button", name="Plan run…").click()
    expect(page.get_by_text(re.compile(r"4 to enhance"))).to_be_visible()
    shot(page, "batch-01-plan")
    page.get_by_role("button", name="Start Batch").click()
    expect(page).to_have_url(re.compile(r"/runs/brn_[a-z0-9]+"))
    expect(page.get_by_text("Prompts awaiting confirmation")).to_be_visible(timeout=20000)
    page.get_by_role("button", name="Select all").click()
    page.get_by_role("button", name=re.compile(r"Confirm 4 prompts \+ generate \(3 Jobs\)")).click()
    expect(page.get_by_text("Candidates awaiting approval")).to_be_visible(timeout=30000)
    shot(page, "batch-02-candidates")
    page.get_by_role("button", name=re.compile(r"Propose best recommended")).click()
    page.get_by_label("approve non-recommended").click()
    page.get_by_role("button", name=re.compile(r"Approve \d+ chosen")).click()
    expect(page.get_by_text(re.compile(r"Review: 4 approved, 0 undecided"))).to_be_visible(timeout=20000)
    page.get_by_role("button", name="Select all").click()
    page.get_by_role("button", name=re.compile(r"Build 4 approved \(3 Jobs\)")).click()
    expect(page.get_by_text(re.compile(r"Builds: 4 valid"))).to_be_visible(timeout=30000)
    for name in ("Tavern", "Harbour", "Sword", "Mill"):
        page.get_by_label(f"accept {name}").click()
    page.get_by_role("button", name="Accept 4 valid results").click()
    expect(page.get_by_role("button", name="Publish 4 accepted")).to_be_visible(timeout=20000)
    expect(page.get_by_text("Execution · model passes")).to_be_visible()
    shot(page, "batch-03-run")
    passes = httpx.get(f"{studio_url}/api/v2/passes").json()["passes"]
    enhance = [p for p in passes if p["residency"].startswith("aux.vlm") and len(p["task_ids"]) == 4]
    assert enhance and len(enhance[0]["jobs"]) == 3  # one VLM residency served all three Jobs
    assert not [e for e in page.errors if "favicon" not in e], page.errors  # type: ignore[attr-defined]
    assert page.external == [], f"offline violation: {page.external}"  # type: ignore[attr-defined]
