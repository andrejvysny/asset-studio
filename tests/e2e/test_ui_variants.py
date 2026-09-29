"""Assets library (group by family), Asset detail variant actions and the Create-variants wizard, SIMULATED engine.
Publishing variants is a Job flow covered elsewhere; here the wizard is driven up to saving Jobs (no inference)."""
from __future__ import annotations

import re
import uuid

import httpx
import pytest
import trimesh
from playwright.sync_api import expect

from tests.e2e.conftest import shot

pytestmark = pytest.mark.e2e
H = {"x-assetstudio": "1"}
NAME = "Variants UI"


def _glb() -> bytes:
    return bytes(trimesh.creation.box().export(file_type="glb"))


def _import_glb(url: str, pid: str, name: str) -> str:
    data = _glb()
    prev = httpx.post(f"{url}/api/v1/projects/{pid}/imports:preview", files={"file": (f"{name}.glb", data)}, headers=H).json()
    assert prev["ok"], prev
    out = httpx.post(f"{url}/api/v1/projects/{pid}/imports:commit", headers=H, json={
        "import_id": prev["import_id"], "name": name, "kind": prev["suggested_kind"], "idempotency_key": f"imp-{name}-e2e"})
    assert out.status_code in (200, 201), out.text
    return out.json()["asset_id"]


@pytest.fixture
def source(studio_url: str) -> tuple[str, str]:
    """A fresh project with one imported GLB, per test (families are created by saving variants)."""
    pid = httpx.post(f"{studio_url}/api/v1/projects", json={"name": f"{NAME} {uuid.uuid4().hex[:6]}"}, headers=H).json()["id"]
    return pid, _import_glb(studio_url, pid, "Crate")


def _direct_rows(page, heights: list[str]) -> None:
    page.get_by_role("radio", name=re.compile("Direct size transform")).click()
    for i, h in enumerate(heights, start=1):
        page.get_by_role("button", name="+ Add row").click()
        page.get_by_label(f"row {i} name").fill(f"Height {h}")
        page.get_by_label(f"row {i} target height (m)").fill(h)
        page.get_by_label(f"row {i} units are meters").check()


def test_detail_actions_and_wizard_saves_batch(page, studio_url: str, source: tuple[str, str]) -> None:
    pid, asset = source
    page.goto(f"/p/{pid}/assets/{asset}")
    expect(page.get_by_role("button", name="New variant")).to_be_visible()
    expect(page.get_by_role("button", name="Export current")).to_be_disabled()
    expect(page.get_by_role("button", name="Download files")).to_be_visible()
    expect(page.get_by_role("button", name="Copy JSON")).to_be_visible()
    shot(page, "variants-01-detail")
    page.get_by_role("button", name="Create variants…").click()
    expect(page).to_have_url(re.compile(rf"/assets/{asset}/variants\?version=ver_[a-z0-9]+&count=6"))
    expect(page.get_by_role("heading", name="Create variants")).to_be_visible()
    expect(page.get_by_text("Each variant is published as a new asset. The source and its versions are not changed.")).to_be_visible()
    expect(page.get_by_text("New family. This asset becomes its anchor.")).to_be_visible()
    expect(page.locator("nav.sidebar a.nav-item.on")).to_contain_text("Assets")
    _direct_rows(page, ["2", "3"])
    expect(page.get_by_text("2 transforms · no image-model calls")).to_be_visible(timeout=10000)
    expect(page.get_by_text(re.compile("The Jobs are grouped into a new draft Batch"))).to_be_visible()
    shot(page, "variants-02-wizard-direct")
    page.get_by_role("button", name="Save 2 Jobs + draft Batch").click()
    expect(page).to_have_url(re.compile(r"/batches/bch_[a-z0-9]+"), timeout=15000)
    batch_id = page.url.rsplit("/", 1)[-1]
    batch = httpx.get(f"{studio_url}/api/v2/projects/{pid}/batches/{batch_id}").json()
    assert len(batch["jobs_detail"]) == 2
    assert httpx.get(f"{studio_url}/api/v2/tasks", params={"project_id": pid}).json()["tasks"] == []  # nothing ran
    assert not [e for e in page.errors if "favicon" not in e], page.errors  # type: ignore[attr-defined]
    assert page.external == [], page.external  # type: ignore[attr-defined]


def test_single_variant_lands_on_job_and_joins_family(page, studio_url: str, source: tuple[str, str]) -> None:
    pid, asset = source
    page.goto(f"/p/{pid}/assets/{asset}")
    page.get_by_role("button", name="New variant").click()
    expect(page).to_have_url(re.compile(r"count=1"))
    expect(page.get_by_role("heading", name="New variant")).to_be_visible()
    page.get_by_label("family name").fill("Crate family")
    page.get_by_role("radio", name=re.compile("Direct size transform")).click()
    expect(page.get_by_label("row 1 name")).to_be_visible()  # the draft starts with one placeholder row
    page.get_by_label("row 1 target height (m)").fill("1.5")
    # unit confirmation gates saving
    expect(page.get_by_role("button", name="Save Job")).to_be_disabled()
    expect(page.get_by_text("confirm that the source uses metres")).to_be_visible()
    page.get_by_label("row 1 units are meters").check()
    expect(page.get_by_text("1 transform · no image-model calls")).to_be_visible(timeout=10000)
    page.get_by_role("button", name="Save Job").click()
    expect(page).to_have_url(re.compile(r"/jobs/job_[a-z0-9]+"), timeout=15000)

    page.goto(f"/p/{pid}/assets/{asset}")
    expect(page.get_by_role("link", name="family · Crate family")).to_be_visible()
    page.get_by_role("link", name="family · Crate family").click()
    expect(page).to_have_url(re.compile(r"/assets\?family=fam_"))
    expect(page.get_by_role("button", name="remove family filter")).to_have_text(re.compile("Family: Crate family"))
    expect(page.get_by_text("1 asset", exact=False).first).to_be_visible()


def test_library_group_by_family_toggle_persists_in_url(page, studio_url: str, source: tuple[str, str]) -> None:
    pid, asset = source
    # a saved variant plan creates the family and attaches the source as its anchor
    d = httpx.get(f"{studio_url}/api/v1/projects/{pid}/assets/{asset}").json()
    ver = d["manifest"]["current_version_id"]
    draft = httpx.post(f"{studio_url}/api/v1/projects/{pid}/variant-drafts", headers=H, json={
        "asset_id": asset, "version_id": ver, "method": "direct_transform", "idempotency_key": "e2e-fam-draft",
        "rows": [{"label": "Big", "glb_transform": {"op": "uniform_scale", "factor": 2.0}}]}).json()
    r = httpx.post(f"{studio_url}/api/v1/projects/{pid}/variant-drafts/{draft['id']}:create-jobs", headers=H,
                   json={"expected_revision": draft["revision"], "idempotency_key": "e2e-fam-jobs"})
    assert r.status_code == 201, r.text

    page.goto(f"/p/{pid}/assets")
    expect(page.get_by_role("switch", name="Group by family")).to_have_attribute("aria-checked", "false")
    page.get_by_role("switch", name="Group by family").click()
    expect(page).to_have_url(re.compile(r"group=family"))
    tile = page.get_by_role("button", name=re.compile(r"^family Crate, 1 asset"))
    expect(tile).to_be_visible(timeout=10000)
    shot(page, "variants-03-library-grouped")
    page.reload()
    expect(page.get_by_role("switch", name="Group by family")).to_have_attribute("aria-checked", "true")
    expect(tile).to_be_visible(timeout=10000)
    tile.click()
    expect(page).to_have_url(re.compile(r"family=fam_"))
    expect(page.get_by_role("button", name="remove family filter")).to_have_text(re.compile(r"Family: Crate"))
    expect(page.get_by_role("link", name=re.compile("Crate"))).to_be_visible()
    page.get_by_role("button", name="remove family filter").click()
    expect(page).not_to_have_url(re.compile("family="))
    page.get_by_role("switch", name="Group by family").click()
    expect(page).not_to_have_url(re.compile("group="))


def test_structural_wizard_references_suggestions_and_summary(page, studio_url: str, source: tuple[str, str]) -> None:
    pid, asset = source
    page.goto(f"/p/{pid}/assets/{asset}")
    page.get_by_role("button", name="Create variants…").click()
    page.get_by_role("radio", name=re.compile("Structural reconstruction")).click()
    expect(page.get_by_text("experimental", exact=True).first).to_be_visible()
    expect(page.get_by_role("radio", name="Related")).to_have_attribute("aria-checked", "true")
    page.get_by_role("radio", name="Exploratory").click()
    expect(page.get_by_text("Larger departures. Identity may drift; QA flags low resemblance.")).to_be_visible()
    # source reference views are rendered automatically; the primary one is selectable
    views = page.get_by_role("group", name="reference views").get_by_role("button")
    expect(views).to_have_count(3, timeout=20000)
    expect(views.first).to_be_visible()
    page.get_by_role("button", name=re.compile("Rear")).click()
    expect(page.get_by_role("button", name=re.compile("Rear view|Rear · primary"))).to_be_visible()
    # suggestions run as a planning task; whichever way the simulated aux ends, the UI reports it and stays usable
    page.get_by_label("describe the set").fill("three variants: taller, squatter, damaged")
    page.get_by_role("button", name="Suggest rows").click()
    expect(page.get_by_role("button", name="Use these rows").or_(page.get_by_text(re.compile("Suggestion failed")))).to_be_visible(timeout=30000)
    if page.get_by_role("button", name="Use these rows").is_visible():
        page.get_by_role("button", name="Use these rows").click()
        expect(page.get_by_label("row 1 change request")).not_to_have_value("")
    else:
        page.get_by_role("button", name="+ Add row").click()
        page.get_by_label("row 1 change request").fill("taller")
    page.get_by_role("button", name="6", exact=True).click()
    expect(page.get_by_text(re.compile(r"variants? × 6 candidates = \d+ image edits · up to \d+ 3D builds after approval"))).to_be_visible(timeout=10000)
    shot(page, "variants-04-structural")
    page.get_by_role("button", name=re.compile(r"^Save (\d+ Jobs \+ draft Batch|Job)$")).click()
    expect(page).to_have_url(re.compile(r"/(batches/bch|jobs/job)_[a-z0-9]+"), timeout=20000)
    assert not [e for e in page.errors if "favicon" not in e], page.errors  # type: ignore[attr-defined]
