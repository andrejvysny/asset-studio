"""The job workspace shows and edits one prompt per preview slot. SIMULATED engine (read-only generation path)."""
from __future__ import annotations

import re
import time
import uuid

import httpx
import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import shot

pytestmark = pytest.mark.e2e
H = {"x-assetstudio": "1"}


def _enhanced_job(studio_url: str) -> tuple[str, str]:
    base = f"{studio_url}/api/v1/projects"
    pid = httpx.post(base, json={"name": f"E2E {uuid.uuid4().hex[:6]}"}, headers=H).json()["id"]
    cfg = httpx.get(f"{base}/{pid}/config").json()
    c = cfg["config"]
    c["categories"] = [{"id": "concept", "slug": "concept", "label": "Concept", "defaults": {"kind": "concept_art"}}]
    httpx.patch(f"{base}/{pid}/config", json={"expected_revision": cfg["revision"], "config": c}, headers=H).raise_for_status()
    v2 = f"{studio_url}/api/v2/projects/{pid}"
    job = httpx.post(f"{v2}/jobs", headers=H, json={
        "title": "Variants", "category_id": "concept", "idempotency_key": f"e2e-var-{uuid.uuid4().hex[:8]}",
        "candidate_count": 4, "items": [{"name": "Tavern", "brief": "warm tavern with long tables"}]}).json()["job"]["id"]
    item = httpx.get(f"{v2}/jobs/{job}").json()["items"][0]
    httpx.post(f"{v2}/jobs/{job}:enhance", headers=H,
               json={"item_ids": [item["id"]], "idempotency_key": f"e2e-enh-{uuid.uuid4().hex[:8]}"}).raise_for_status()
    for _ in range(100):
        if len(httpx.get(f"{v2}/jobs/{job}").json()["items"][0]["prompt_variants"]) == 4:
            return pid, job
        time.sleep(0.2)
    raise AssertionError("enhancement did not produce 4 prompt variants")


def test_four_preview_prompts_are_visible_and_editable(page, studio_url: str) -> None:
    pid, job = _enhanced_job(studio_url)
    page.goto(f"/p/{pid}/jobs/{job}")
    main = page.get_by_label("prompt", exact=True)
    expect(main).to_have_value(re.compile("simulated enhancement"), timeout=15000)
    toggle = page.get_by_role("button", name=re.compile(r"Show the other 3 preview prompts"))
    toggle.click()
    for n in (2, 3, 4):
        expect(page.get_by_label(f"prompt for preview {n}")).to_be_visible()
    texts = {main.input_value(), *(page.get_by_label(f"prompt for preview {n}").input_value() for n in (2, 3, 4))}
    assert len(texts) == 4  # four different prompts
    shot(page, "prompt_variants")

    box = page.get_by_label("prompt for preview 3")
    box.fill("a tavern seen from above, lanterns lit")
    page.get_by_role("button", name="Save prompt 3").click()
    expect(page.get_by_text("Preview 3 prompt · edited")).to_be_visible(timeout=10000)
    expect(page.get_by_label("prompt for preview 3")).to_have_value("a tavern seen from above, lanterns lit")
