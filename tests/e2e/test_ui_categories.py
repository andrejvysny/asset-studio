"""Assign, move, bulk-move and clear asset categories from the library UI. SIMULATED engine."""
from __future__ import annotations

import io
import re
import uuid

import httpx
import pytest
from PIL import Image
from playwright.sync_api import expect

from tests.e2e.conftest import shot

pytestmark = pytest.mark.e2e
H = {"x-assetstudio": "1"}


def _setup(studio_url: str) -> tuple[str, list[str]]:
    base = f"{studio_url}/api/v1/projects"
    pid = httpx.post(base, json={"name": f"E2E {uuid.uuid4().hex[:6]}"}, headers=H).json()["id"]
    cfg = httpx.get(f"{base}/{pid}/config").json()
    c = cfg["config"]
    c["categories"] = [{"id": "props", "slug": "props", "label": "Props"}, {"id": "terrain", "slug": "terrain", "label": "Terrain"}]
    httpx.patch(f"{base}/{pid}/config", json={"expected_revision": cfg["revision"], "config": c}, headers=H).raise_for_status()
    ids = []
    for i, name in enumerate(("Barrel", "Crate")):
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (40 * (i + 1), 80, 120)).save(buf, "PNG")
        prev = httpx.post(f"{base}/{pid}/imports:preview", files={"file": (f"{name}.png", buf.getvalue())}, headers=H).json()
        ids.append(httpx.post(f"{base}/{pid}/imports:commit", headers=H, json={
            "import_id": prev["import_id"], "name": name, "kind": "concept_art",
            "idempotency_key": f"e2e-cat-000{i}"}).json()["asset_id"])
    return pid, ids


def test_change_category_on_detail_and_bulk_move_in_library(page, studio_url: str) -> None:
    pid, ids = _setup(studio_url)
    page.goto(f"/p/{pid}/assets/{ids[0]}")
    expect(page.get_by_label("category", exact=True)).to_contain_text("Uncategorized")
    page.get_by_role("button", name="Change category…").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("search categories").fill("pro")
    dialog.get_by_role("option", name=re.compile("Props")).click()
    expect(dialog).to_contain_text("will move to Props")
    dialog.get_by_role("button", name="Apply").click()
    expect(page.get_by_label("category", exact=True)).to_contain_text("Props", timeout=10000)

    page.goto(f"/p/{pid}/assets")
    page.get_by_role("button", name="Select", exact=True).click()
    page.get_by_role("checkbox", name="select Barrel").click()
    page.get_by_role("checkbox", name="select Crate").click()
    expect(page.get_by_text("2 selected")).to_be_visible()
    page.get_by_role("button", name="Move to category…").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("option", name=re.compile("Terrain")).click()
    expect(dialog).to_contain_text("2 assets will move to Terrain")
    shot(page, "bulk_move_category")
    dialog.get_by_role("button", name="Move 2 assets").click()
    expect(page.get_by_role("button", name=re.compile(r"^Terrain\s*2$"))).to_be_visible(timeout=10000)
    expect(page.get_by_role("button", name=re.compile(r"^Props\s*0$"))).to_be_visible()

    page.get_by_role("checkbox", name="select Barrel").click()  # clear: back to Uncategorized
    page.get_by_role("button", name="Move to category…").click()
    page.get_by_role("dialog").get_by_role("option", name="Uncategorized").click()
    page.get_by_role("dialog").get_by_role("button", name=re.compile("Apply|Move")).click()
    expect(page.get_by_role("button", name=re.compile(r"^Terrain\s*1$"))).to_be_visible(timeout=10000)
    cat = httpx.get(f"{studio_url}/api/v1/projects/{pid}/assets/{ids[0]}").json()["manifest"]["category_id"]
    assert cat is None
