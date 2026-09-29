"""Single-asset Job workspace in the browser (SIMULATED engines): rounds and approval from any round, build ->
accept -> publish, and a direct-transform variant Job. Jobs are created through the API so these tests only depend
on the workspace itself."""
from __future__ import annotations

import io
import re

import httpx
import pytest
import trimesh
from playwright.sync_api import expect

from tests.e2e.conftest import shot

pytestmark = pytest.mark.e2e
H = {"x-assetstudio": "1"}
HEIGHT2 = {"op": "target_height", "height_m": 2.0, "anchor": "bottom_center", "units_confirmed": True}


def _project(url: str, name: str) -> str:
    pid = httpx.post(f"{url}/api/v1/projects", json={"name": name}, headers=H).json()["id"]
    cfg = httpx.get(f"{url}/api/v1/projects/{pid}/config").json()["config"]
    cfg["categories"] = [{"id": "concept", "slug": "concept", "label": "Concept", "defaults": {"kind": "concept_art"}}]
    r = httpx.patch(f"{url}/api/v1/projects/{pid}/config", json={"expected_revision": 1, "config": cfg}, headers=H)
    assert r.status_code == 200, r.text
    return pid


def _job(url: str, pid: str, title: str) -> str:
    r = httpx.post(f"{url}/api/v2/projects/{pid}/jobs", headers=H, json={
        "title": title, "category_id": "concept", "idempotency_key": f"idem-{pid}-{title}", "source": "manual",
        "candidate_count": 3, "items": [{"name": title, "brief": f"a {title.lower()}, moody light"}]})
    assert r.status_code in (200, 201), r.text
    return r.json()["job"]["id"]


def _approve_anyway(page) -> None:
    if page.get_by_role("dialog").is_visible():
        page.get_by_role("button", name="Approve anyway").click()


def test_rounds_approve_from_any_round_build_and_publish(page, studio_url: str) -> None:
    pid = _project(studio_url, "Workspace rounds")
    jid = _job(studio_url, pid, "Old lighthouse")
    page.goto(f"/p/{pid}/jobs/{jid}")
    expect(page).to_have_url(re.compile(r"/jobs/job_[a-z0-9]+/prompt$"))
    expect(page.get_by_text("← Jobs / ")).to_be_visible()
    expect(page.get_by_text("Not in a Batch")).to_be_visible()
    expect(page.get_by_text("No rounds yet. Run enhances the prompt")).to_be_visible()
    page.get_by_role("button", name="Run · enhance prompt").click()
    prompt = page.get_by_label("prompt", exact=True)
    expect(prompt).to_have_value(re.compile("simulated enhancement"), timeout=15000)
    expect(page.get_by_text("Prompt · round 1 · confirm to generate")).to_be_visible()
    shot(page, "ws-01-prompt")
    page.get_by_role("button", name="Confirm prompt + generate 3 candidates").click()
    expect(page.get_by_role("button", name=re.compile(r"^candidate 3 of round 1"))).to_be_visible(timeout=20000)
    expect(page.get_by_text("QA pending")).to_have_count(0, timeout=20000)
    expect(page.get_by_text("QA · R1 #1 · advisory")).to_be_visible()  # focus is not approval
    expect(page.get_by_role("button", name="Approve R1 #1")).to_be_visible()
    page.keyboard.press("2")
    _approve_anyway(page)
    expect(page.get_by_role("button", name="Approved ✓ · click to undo")).to_be_visible(timeout=10000)
    expect(page.get_by_role("button", name=re.compile(r"^R1 .*✓ #2"))).to_be_visible()
    shot(page, "ws-02-approved")

    prompt.fill(prompt.input_value() + " with a warm lantern")
    expect(page.get_by_text("edited · not generated")).to_be_visible()
    page.get_by_role("button", name=re.compile(r"^Generate round 2 · edited prompt")).click()
    expect(page.get_by_role("button", name=re.compile(r"^candidate 1 of round 2"))).to_be_visible(timeout=20000)
    expect(page.get_by_text(re.compile(r"\d+ words added vs R1"))).to_be_visible()
    expect(page.get_by_text("QA pending")).to_have_count(0, timeout=20000)
    shot(page, "ws-03-round2")

    page.get_by_role("button", name=re.compile(r"^R1")).click()  # arrow keys move between rounds too
    page.get_by_role("button", name=re.compile(r"^candidate 1 of round 1")).click()
    page.get_by_role("button", name="Switch approval to R1 #1").click()
    _approve_anyway(page)
    expect(page.get_by_role("button", name=re.compile(r"^R1 .*✓ #1"))).to_be_visible(timeout=10000)
    page.get_by_role("button", name=re.compile(r"^Upscale →")).click()
    expect(page).to_have_url(re.compile(r"/build$"))
    page.get_by_role("button", name="Accept attempt 1", exact=True).click(timeout=15000)
    expect(page.get_by_role("button", name=re.compile(r"^Accepted attempt 1"))).to_be_visible(timeout=10000)
    shot(page, "ws-04-build")
    page.get_by_role("link", name=re.compile(r"Publish$")).click()
    expect(page).to_have_url(re.compile(r"/publish$"))
    expect(page.get_by_text("new asset · v1")).to_be_visible(timeout=10000)
    expect(page.get_by_text("attempt 1 ·")).to_be_visible()
    page.get_by_role("button", name="Publish", exact=True).click()
    expect(page.get_by_role("button", name="Published ✓")).to_be_visible(timeout=15000)
    shot(page, "ws-05-publish")
    for old, new in (("prompts", "prompt"), ("candidates", "prompt"), ("approve", "prompt")):  # old slugs redirect
        page.goto(f"/p/{pid}/jobs/{jid}/{old}")
        expect(page).to_have_url(re.compile(rf"/jobs/{jid}/{new}$"))
    assert not [e for e in page.errors if "favicon" not in e], page.errors  # type: ignore[attr-defined]
    assert page.external == [], f"offline violation: {page.external}"  # type: ignore[attr-defined]


def _glb() -> bytes:
    buf = io.BytesIO()
    trimesh.creation.box().export(buf, file_type="glb")
    return buf.getvalue()


def test_direct_variant_job_run_transform_accept_publish(page, studio_url: str) -> None:
    pid = _project(studio_url, "Workspace direct")
    prev = httpx.post(f"{studio_url}/api/v1/projects/{pid}/imports:preview", headers=H,
                      files={"file": ("crate.glb", _glb())}).json()
    src = httpx.post(f"{studio_url}/api/v1/projects/{pid}/imports:commit", headers=H, json={
        "import_id": prev["import_id"], "name": "Crate", "kind": prev["suggested_kind"], "idempotency_key": "e2e-import-crate"}).json()
    draft = httpx.post(f"{studio_url}/api/v1/projects/{pid}/variant-drafts", headers=H, json={
        "asset_id": src["asset_id"], "version_id": src["version_id"], "method": "direct_transform",
        "idempotency_key": "e2e-draft", "requested_variants": 1, "rows": [{"label": "Two meters", "glb_transform": HEIGHT2}]}).json()
    made = httpx.post(f"{studio_url}/api/v1/projects/{pid}/variant-drafts/{draft['id']}:create-jobs", headers=H, json={
        "expected_revision": draft["revision"], "idempotency_key": "e2e-jobs"})
    assert made.status_code == 201, made.text
    jid = made.json()["job_ids"][0]
    page.goto(f"/p/{pid}/jobs/{jid}/prompt")
    expect(page.get_by_text("Direct size transform")).to_be_visible(timeout=10000)
    expect(page.get_by_text("Same source mesh · direct transform")).to_be_visible()
    expect(page.get_by_text("Scales the source GLB to 2 m height, anchored at bottom centre.")).to_be_visible()
    expect(page.get_by_role("link", name=re.compile(r"^Crate · v1"))).to_be_visible()
    expect(page.get_by_label("prompt", exact=True)).to_have_count(0)  # no prompt for a direct transform
    shot(page, "ws-06-direct")
    page.get_by_role("button", name="Run transform").click()
    expect(page).to_have_url(re.compile(r"/build$"))
    page.get_by_role("button", name="Accept attempt 1", exact=True).click(timeout=20000)
    expect(page.get_by_text("height check")).to_be_visible()
    expect(page.get_by_role("button", name=re.compile(r"^Accepted attempt 1"))).to_be_visible(timeout=10000)
    page.get_by_role("link", name=re.compile(r"Publish$")).click()
    expect(page.get_by_text("Derived from")).to_be_visible(timeout=10000)
    expect(page.get_by_role("link", name=re.compile(r"^Crate · v1"))).to_be_visible()
    page.get_by_role("button", name="Publish", exact=True).click()
    expect(page.get_by_role("button", name="Published ✓")).to_be_visible(timeout=15000)
    shot(page, "ws-07-direct-published")
    assert not [e for e in page.errors if "favicon" not in e], page.errors  # type: ignore[attr-defined]


def test_legacy_multi_item_job_shows_item_picker(page, studio_url: str) -> None:
    pid = _project(studio_url, "Workspace legacy")
    r = httpx.post(f"{studio_url}/api/v2/projects/{pid}/jobs", headers=H, json={
        "title": "Two scenes", "category_id": "concept", "idempotency_key": f"idem-{pid}-multi", "source": "manual",
        "items": [{"name": "Tavern", "brief": "a warm tavern"}, {"name": "Harbour", "brief": "a small harbour"}]})
    assert r.status_code in (200, 201), r.text
    page.goto(f"/p/{pid}/jobs/{r.json()['job']['id']}/prompt")
    expect(page.get_by_text("Items · 2")).to_be_visible(timeout=10000)
    expect(page.get_by_text("a warm tavern")).to_be_visible()
    page.get_by_role("link", name=re.compile("^Harbour")).click()
    expect(page).to_have_url(re.compile(r"/prompt\?item=itm_"))
    expect(page.get_by_text("a small harbour")).to_be_visible()
    page.get_by_role("link", name=re.compile(r"Publish$")).click()
    expect(page).to_have_url(re.compile(r"/publish\?item=itm_"))
