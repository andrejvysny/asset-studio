"""Jobs list, New Job (one asset + references), and Shot list -> Jobs in the browser. SIMULATED engine."""
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


def _project(url: str, name: str) -> str:
    pid = httpx.post(f"{url}/api/v1/projects", json={"name": name}, headers=H).json()["id"]
    cfg = httpx.get(f"{url}/api/v1/projects/{pid}/config").json()["config"]
    cfg["categories"] = [{"id": "concept", "slug": "concept", "label": "Concept", "defaults": {"kind": "concept_art"}},
                         {"id": "icons", "slug": "icons", "label": "Icons", "defaults": {"kind": "icon"}}]
    r = httpx.patch(f"{url}/api/v1/projects/{pid}/config", json={"expected_revision": 1, "config": cfg}, headers=H)
    assert r.status_code == 200, r.text
    return pid


def _job(url: str, pid: str, title: str, cat: str = "concept") -> str:
    r = httpx.post(f"{url}/api/v2/projects/{pid}/jobs", headers=H, json={
        "title": title, "category_id": cat, "idempotency_key": f"idem-{pid}-{title}", "source": "manual",
        "items": [{"name": title, "brief": f"a {title.lower()}"}]})
    assert r.status_code in (200, 201), r.text
    return r.json()["job"]["id"]


def _png(path: Path) -> Path:
    buf = io.BytesIO()
    Image.new("RGB", (120, 80), (200, 120, 40)).save(buf, "PNG")
    path.write_bytes(buf.getvalue())
    return path


def test_new_single_asset_job_with_reference_and_creative_preset(page, studio_url: str, tmp_path: Path) -> None:
    pid = _project(studio_url, "NewJob refs")
    page.goto(f"/p/{pid}/jobs/new?cat=concept")
    expect(page.get_by_role("heading", name="What are you making?")).to_be_visible()
    expect(page.get_by_text("← Jobs / New Job · one asset")).to_be_visible()
    page.locator("label.field", has_text="Name").locator("input").fill("Lighthouse")
    page.get_by_label("Brief").fill("a lighthouse on a cliff")
    page.get_by_label("upload reference image").set_input_files(str(_png(tmp_path / "ref.png")))
    img = page.get_by_role("img", name="ref.png")
    expect(img).to_be_visible()
    page.get_by_label("note for ref.png").fill("the warm colour palette")
    page.get_by_role("button", name="Mark region that matters").click()
    box = img.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] * 0.25, box["y"] + box["height"] * 0.25)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.75, box["y"] + box["height"] * 0.75, steps=4)
    page.mouse.up()
    expect(page.get_by_role("button", name="✓ Region marked · clear")).to_be_visible()
    page.get_by_role("button", name="Creative").click()
    expect(page.get_by_text("Additions are marked for review.")).to_be_visible()
    shot(page, "jl-01-new-job")
    page.get_by_role("button", name="Save Job", exact=True).click()
    expect(page).to_have_url(re.compile(r"/jobs/job_[a-z0-9]+"))
    job_id = page.url.split("/jobs/")[1].split("/")[0]
    job = httpx.get(f"{studio_url}/api/v2/projects/{pid}/jobs/{job_id}").json()
    assert len(job["items"]) == 1
    item = job["items"][0]
    assert item["enhance_preset"] == "creative"
    [ref] = item["references"]
    assert ref["note"] == "the warm colour palette"
    assert ref["crop"] is not None and 0.15 < ref["crop"]["w"] < 0.85
    assert httpx.get(f"{studio_url}/api/v2/tasks", params={"project_id": pid}).json()["tasks"] == []  # saved, not run


def test_jobs_group_by_and_selection_actions(page, studio_url: str) -> None:
    pid = _project(studio_url, "Jobs list")
    a, b, _c = (_job(studio_url, pid, t) for t in ("Alpha", "Bravo", "Charlie"))
    page.goto(f"/p/{pid}/jobs")
    expect(page.get_by_text("One Job = one asset · prompt → candidates → approve → build → publish")).to_be_visible()
    expect(page.get_by_text("Drafts · not run")).to_be_visible()
    group = page.get_by_role("group", name="group by")
    group.get_by_role("button", name="Family").click()
    expect(page).to_have_url(re.compile(r"group=family"))
    expect(page.get_by_text("No family")).to_be_visible()
    group.get_by_role("button", name="Kind").click()
    expect(page.get_by_text("Concept art", exact=True).first).to_be_visible()
    group.get_by_role("button", name="None").click()
    expect(page.get_by_role("rowheader")).to_have_count(0)
    group.get_by_role("button", name="Batch").click()
    expect(page.get_by_text("Not in a Batch")).to_be_visible()
    page.get_by_label("select Alpha").click()
    page.get_by_label("select Bravo").click()
    expect(page.get_by_text("2 selected")).to_be_visible()
    page.get_by_role("button", name="Create Batch").click()
    expect(page).to_have_url(re.compile(r"/batches/bch_[a-z0-9]+"))
    batches = httpx.get(f"{studio_url}/api/v2/projects/{pid}/batches").json()["batches"]
    assert sorted(batches[0]["job_ids"]) == sorted([a, b])
    page.goto(f"/p/{pid}/jobs?group=batch")
    expect(page.get_by_text(re.compile(r"Batch of 2 Jobs · "))).to_be_visible()
    # Move Charlie into the existing Batch; then move Alpha out to a second Batch: a Job keeps one Batch
    page.get_by_label("select Charlie").click()
    page.get_by_role("button", name="Add to Batch ▾").click()
    expect(page.get_by_text("Move 1 into")).to_be_visible()
    page.get_by_role("button", name=re.compile(r"^Batch of 2 Jobs · ")).click()
    expect(page.get_by_text("Not in a Batch")).to_be_hidden(timeout=10000)
    shot(page, "jl-02-jobs-batch-group")
    # Run standalone on one Job
    group = page.get_by_role("group", name="group by")
    group.get_by_role("button", name="Stage").click()
    page.get_by_label("select Bravo").click()
    page.get_by_role("button", name="Run standalone").click()
    expect(page.get_by_role("status").get_by_text(re.compile(r"started brn_"))).to_be_visible(timeout=10000)
    expect(page.get_by_text("Waiting on you", exact=True)).to_be_visible(timeout=30000)
    tasks = httpx.get(f"{studio_url}/api/v2/tasks", params={"project_id": pid}).json()["tasks"]
    assert tasks and {t["job_id"] for t in tasks} == {b}
    shot(page, "jl-03-jobs-stage-group")
    assert not [e for e in page.errors if "favicon" not in e], page.errors  # type: ignore[attr-defined]


def test_shot_list_rows_become_one_job_each_and_a_batch(page, studio_url: str) -> None:
    pid = _project(studio_url, "Shots to jobs")
    rows = [{"id": None, "name": n, "category_id": c, "kind": None, "brief": f"a {n.lower()}", "priority": "med",
             "notes": "", "target_asset_id": None, "external_id": None, "archived": False}
            for n, c in (("Well", "concept"), ("Key icon", "icons"), ("Cart", "concept"))]
    cur = httpx.get(f"{studio_url}/api/v1/projects/{pid}/shot-list").json()
    r = httpx.put(f"{studio_url}/api/v1/projects/{pid}/shot-list", headers=H,
                  json={"expected_revision": cur["revision"], "items": rows})
    assert r.status_code == 200, r.text
    page.goto(f"/p/{pid}/shots")
    expect(page.get_by_role("button", name="Select rows")).to_be_visible()
    page.get_by_label("select all planned").click()
    expect(page.get_by_role("button", name="Create 3 Jobs + Batch")).to_be_visible()
    expect(page.get_by_text("Each selected row becomes its own Job")).to_be_visible()
    shot(page, "jl-04-shots")
    page.get_by_role("button", name="Create 3 Jobs + Batch").click()
    expect(page).to_have_url(re.compile(r"/batches/bch_[a-z0-9]+"))
    jobs = httpx.get(f"{studio_url}/api/v2/projects/{pid}/jobs").json()["jobs"]
    assert sorted(j["title"] for j in jobs) == ["Cart", "Key icon", "Well"]
    assert all(j["counts"]["items"] == 1 for j in jobs)
    assert httpx.get(f"{studio_url}/api/v2/tasks", params={"project_id": pid}).json()["tasks"] == []
    page.goto(f"/p/{pid}/shots")
    expect(page.get_by_text(re.compile(r"^job "))).to_have_count(3)
    expect(page.get_by_role("button", name="Select rows")).to_be_visible()
