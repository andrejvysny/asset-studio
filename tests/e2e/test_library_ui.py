"""Asset library UI against an isolated library server with fixture data (no GPU, no real outputs touched)."""
from __future__ import annotations

import json
import re
import urllib.request
from typing import TYPE_CHECKING

from playwright.sync_api import expect

if TYPE_CHECKING:
    from conftest import StudioPage

SLOT = "forest_prop_containers_storage_a"


def _api(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path) as r:
        return json.loads(r.read())


def test_shell_and_navigation(fx_page: StudioPage) -> None:
    page = fx_page.goto("/")
    expect(page).to_have_url(re.compile(r"/library$"))
    for label, url, heading in [("Coverage", "/coverage", "Catalog coverage"), ("Jobs", "/jobs", "Jobs"),
                                ("New job", "/new", "New job"), ("Runtime", "/runtime", "Runtime"),
                                ("Browse", "/library", "Temperate forest")]:
        page.get_by_role("link", name=re.compile(f"^{label}")).click()
        expect(page).to_have_url(re.compile(url + "$"))
        expect(page.get_by_text(heading, exact=True).first).to_be_visible()
    # licence banner comes from config/licences.yaml (nvdiffrast not cleared)
    page.get_by_role("button", name=re.compile("not cleared for commercial use")).click()
    expect(page).to_have_url(re.compile("/runtime$"))
    expect(page.get_by_text("not cleared").first).to_be_visible()
    fx_page.shot("runtime")


def test_library_biomes_layers_views(fx_page: StudioPage) -> None:
    page = fx_page.goto("/library/forest")
    expect(page.get_by_text("forest_* · 541 base meshes")).to_be_visible()
    page.get_by_text("Misty marsh").click()
    expect(page).to_have_url(re.compile("/library/marsh$"))
    expect(page.get_by_text("119 base meshes", exact=False)).to_be_visible()
    page.get_by_text("5 · Props").click()
    expect(page.get_by_text("clay jars + pots")).to_be_visible()
    expect(page.get_by_text("mud flats")).to_have_count(0)  # other layers filtered out
    fx_page.shot("marsh_props_grid")
    page.get_by_role("button", name=re.compile("Family table")).click()
    expect(page.get_by_text("Variants · states")).to_be_visible()
    expect(page.locator(".tr")).to_have_count(3)  # 3 prop families in marsh
    fx_page.shot("marsh_props_table")


def test_assign_filter_and_coverage(fx_page: StudioPage, fixture_server: dict) -> None:
    base = fixture_server["base"]
    page = fx_page.goto(f"/slots/{SLOT}")
    expect(page.get_by_text("planned", exact=True)).to_be_visible()
    expect(page.get_by_text("E2E barrel · att-01")).to_be_visible()  # unassigned approved result offered
    page.get_by_role("button", name="Assign here").click()
    expect(page.get_by_text("assigned", exact=True)).to_be_visible()
    expect(page.locator("model-viewer")).to_have_count(1, timeout=15000)  # lazy-loaded 3D viewer
    page.wait_for_function("document.querySelector('model-viewer')?.loaded === true", timeout=20000)
    fx_page.shot("slot_assigned_viewer")
    assert _api(base, f"/api/slots/{SLOT}")["assignment"]["attempt_id"] == fixture_server["attempt_id"]

    # assigned-only filter: exactly this slot, URL keeps the filter, survives reload and biome switch
    page = fx_page.goto("/library/forest")
    page.get_by_role("button", name=re.compile("Assigned only")).click()
    expect(page).to_have_url(re.compile(r"\?show=assigned$"))
    expect(page.locator(f"text={SLOT}")).to_have_count(1)
    expect(page.get_by_text("planned", exact=True)).to_have_count(0)
    fx_page.shot("filter_assigned")
    page.reload()
    page.wait_for_load_state("networkidle")
    expect(page.locator(f"text={SLOT}")).to_have_count(1)
    page.get_by_text("Farmland valleys").click()
    expect(page).to_have_url(re.compile(r"/library/farmland\?show=assigned$"))
    expect(page.get_by_text("No assigned assets in Farmland valleys")).to_be_visible()
    fx_page.shot("filter_empty")
    page.get_by_role("button", name=re.compile("All slots")).click()
    expect(page).to_have_url(re.compile("/library/farmland$"))

    # coverage + biome counters reflect the assignment
    page = fx_page.goto("/coverage")
    expect(page.get_by_text("1 done")).to_have_count(1)
    fx_page.shot("coverage")
    page.get_by_text("Temperate forest").click()
    expect(page).to_have_url(re.compile("/library/forest$"))
    expect(page.get_by_text("1/541")).to_be_visible()

    # unassign from the slot page
    page = fx_page.goto(f"/slots/{SLOT}")
    page.get_by_role("button", name="Unassign").click()
    expect(page.get_by_text("planned", exact=True)).to_be_visible()
    assert _api(base, f"/api/slots/{SLOT}")["assignment"] is None


def test_attempts_screen_and_assign_by_slot_id(fx_page: StudioPage, fixture_server: dict) -> None:
    page = fx_page.goto(f"/attempts/{fixture_server['job_id']}")
    expect(page.get_by_text("E2E barrel — 3D attempts")).to_be_visible()
    expect(page.get_by_text("reload", exact=True)).to_be_visible()  # validation check rows
    expect(page.get_by_text(re.compile(r"2[,.\u202f]?000 \(triangle_floor\)"))).to_be_visible()
    fx_page.shot("attempt_completed")
    page.get_by_placeholder("slot id").fill("forest_prop_containers_storage_b")
    page.get_by_role("button", name="Assign", exact=True).click()
    expect(page.get_by_text("forest_prop_containers_storage_b").first).to_be_visible()
    # unknown slot -> visible error, no crash
    page.get_by_placeholder("slot id").fill("not_a_slot")
    page.get_by_role("button", name="Assign", exact=True).click()
    expect(page.locator(".error")).to_contain_text("unknown slot")
    fx_page.errors = [e for e in fx_page.errors if "404" not in e]  # expected API 404 for the bad slot


def test_unknown_slot_and_deep_links(fx_page: StudioPage) -> None:
    page = fx_page.goto("/slots/does_not_exist")
    expect(page.locator(".error")).to_contain_text("unknown slot")
    fx_page.errors = [e for e in fx_page.errors if "404" not in e]
    page = fx_page.goto("/library/volcanic?show=assigned")
    expect(page.get_by_text("No assigned assets in Volcanic ashlands")).to_be_visible()
