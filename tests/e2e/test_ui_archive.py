"""Asset archive / restore in the library UI. SIMULATED engine."""
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


def _asset(studio_url: str, name: str) -> tuple[str, str]:
    pid = httpx.post(f"{studio_url}/api/v1/projects", json={"name": f"E2E {uuid.uuid4().hex[:6]}"}, headers=H).json()["id"]
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 80, 40)).save(buf, "PNG")
    base = f"{studio_url}/api/v1/projects/{pid}"
    prev = httpx.post(f"{base}/imports:preview", files={"file": (f"{name}.png", buf.getvalue())}, headers=H).json()
    done = httpx.post(f"{base}/imports:commit", headers=H, json={
        "import_id": prev["import_id"], "name": name, "kind": "concept_art", "idempotency_key": "e2e-archive-01"}).json()
    return pid, done["asset_id"]


def test_archive_then_restore_from_ui(page, studio_url: str) -> None:
    pid, asset_id = _asset(studio_url, "Rusty gate")
    page.goto(f"/p/{pid}/assets/{asset_id}")
    page.get_by_role("button", name="Archive", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("restore it later")
    dialog.get_by_role("button", name="Archive", exact=True).click()

    expect(page).to_have_url(re.compile(rf"/p/{pid}/assets$"), timeout=10000)
    expect(page.get_by_text("Rusty gate")).to_have_count(0)  # gone from the active library

    page.get_by_label("show archived").click()
    expect(page.get_by_text("Rusty gate")).to_be_visible(timeout=10000)
    shot(page, "archived_library")
    page.get_by_text("Rusty gate").click()
    expect(page.get_by_text("This asset is archived")).to_be_visible(timeout=10000)
    page.get_by_role("button", name="Restore").click()
    expect(page.get_by_text("This asset is archived")).to_have_count(0, timeout=10000)

    page.goto(f"/p/{pid}/assets")
    expect(page.get_by_text("Rusty gate")).to_be_visible(timeout=10000)


def test_permanent_delete_needs_archive_and_typed_name(page, studio_url: str) -> None:
    pid, asset_id = _asset(studio_url, "Broken crate")
    page.goto(f"/p/{pid}/assets/{asset_id}")
    expect(page.get_by_role("button", name="Delete permanently…")).to_have_count(0)  # not for active assets
    page.get_by_role("button", name="Archive", exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name="Archive", exact=True).click()
    expect(page).to_have_url(re.compile(rf"/p/{pid}/assets$"), timeout=10000)

    page.goto(f"/p/{pid}/assets/{asset_id}")
    page.get_by_role("button", name="Delete permanently…").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("cannot be undone")
    confirm = dialog.get_by_role("button", name="Delete permanently", exact=True)
    expect(confirm).to_be_disabled()  # nothing typed yet
    dialog.get_by_label("confirm name").fill("broken_crate")
    expect(confirm).to_be_enabled()
    shot(page, "delete_dialog")
    confirm.click()
    expect(page).to_have_url(re.compile(rf"/p/{pid}/assets$"), timeout=10000)
    assert httpx.get(f"{studio_url}/api/v1/projects/{pid}/assets/{asset_id}").status_code == 404
