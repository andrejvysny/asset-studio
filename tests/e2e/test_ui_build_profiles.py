"""Typed build profiles in the browser (SIMULATED engines): editor, per-category assignment, effects preview and
the rebuild dialog's reuse line."""
from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import shot

pytestmark = pytest.mark.e2e
H = {"x-assetstudio": "1"}


def _project(url: str, name: str) -> str:
    pid = httpx.post(f"{url}/api/v1/projects", json={"name": name}, headers=H).json()["id"]
    cfg = httpx.get(f"{url}/api/v1/projects/{pid}/config").json()["config"]
    cfg["categories"] = [{"id": "props", "slug": "props", "label": "Props", "defaults": {
        "kind": {"mode": "value", "value": "model3d"}}}]
    r = httpx.patch(f"{url}/api/v1/projects/{pid}/config", json={"expected_revision": cfg["revision"], "config": cfg}, headers=H)
    assert r.status_code == 200, r.text
    return pid


def _config(url: str, pid: str) -> dict:
    return httpx.get(f"{url}/api/v1/projects/{pid}/config").json()["config"]


def _save(page, name: str) -> None:
    with page.expect_response(lambda r: r.request.method == "PATCH" and r.url.endswith("/config")) as done:
        page.get_by_role("button", name=name).click()
    assert done.value.ok, done.value.text()


def test_create_profile_assign_in_schema_and_effects(page, studio_url: str) -> None:
    pid = _project(studio_url, "E2E build profiles")
    page.goto(f"/p/{pid}/pipelines")
    box = page.get_by_label("build profiles")
    prompts = ["Bad Id", "cutout"]
    page.on("dialog", lambda d: d.accept(prompts.pop(0)) if d.type == "prompt" else d.accept())  # alerts: just OK
    box.get_by_role("button", name="+ Profile").click()
    expect(box.get_by_label("profile label")).to_have_count(0)
    box.get_by_role("button", name="+ Profile").click()
    expect(box.get_by_label("profile label")).to_have_value("cutout")
    box.get_by_label("profile label").fill("Cut-out props")
    box.get_by_label("small_components").select_option("preserve")
    expect(box.get_by_label("alpha_cutoff")).to_have_count(0)
    box.get_by_label("alpha_mode").select_option("mask")
    box.get_by_label("alpha_cutoff").fill("0.4")
    box.get_by_label("double_sided").select_option("yes")
    box.get_by_label("roughness_min").fill("0.2")
    _save(page, "Save profiles")
    saved = _config(studio_url, pid)["build_profiles"]["cutout"]
    assert saved["label"] == "Cut-out props"
    assert saved["geometry"]["small_components"] == "preserve" and saved["geometry"]["fill_holes"] is None
    assert saved["material"] == {"alpha_mode": "mask", "alpha_cutoff": 0.4, "double_sided": True, "metallic": None,
                                 "roughness_min": 0.2, "roughness_max": None}
    shot(page, "build_profiles_editor")

    page.goto(f"/p/{pid}/schema")
    page.get_by_role("button", name=re.compile(r"^Props")).click()
    page.get_by_label("Build profile").select_option("cutout")
    _save(page, "Save schema")
    cfg = _config(studio_url, pid)
    assert cfg["categories"][0]["defaults"]["build_profile"] == {"mode": "value", "value": "cutout"}

    page.goto(f"/p/{pid}/pipelines")
    delete = page.get_by_label("build profiles").get_by_role("button", name="Delete profile")
    expect(delete).to_be_disabled(timeout=10000)

    page.goto(f"/p/{pid}/style")
    page.get_by_label("effects category").select_option("props")
    expect(page.get_by_label("effect build_profile.material.alpha_mode applied")).to_be_visible(timeout=10000)
    expect(page.get_by_label("preview selected style")).to_be_checked()


def test_effects_style_preview_toggle(page, studio_url: str) -> None:
    pid = _project(studio_url, "E2E effects style")
    cfg = _config(studio_url, pid)
    cfg["styles"]["ink"] = {"label": "Ink", "guide": "bold ink lines", "negative": "", "palette": []}
    cfg["styles"]["clay"] = {"label": "Clay", "guide": "soft clay", "negative": "", "palette": []}
    cfg["categories"][0]["defaults"]["style"] = {"mode": "value", "value": "clay"}
    r = httpx.patch(f"{studio_url}/api/v1/projects/{pid}/config", json={"expected_revision": cfg["revision"], "config": cfg}, headers=H)
    assert r.status_code == 200, r.text
    page.goto(f"/p/{pid}/style")
    page.get_by_role("button", name="ink", exact=True).click()
    page.get_by_label("effects category").select_option("props")
    expect(page.get_by_label("effect style.guide conditioning_only").get_by_text("bold ink lines")).to_be_visible(timeout=10000)
    page.get_by_label("preview selected style").uncheck()
    expect(page.get_by_label("scope style")).to_have_text("Scope resolves to style: clay", timeout=10000)


def test_rebuild_dialog_shows_what_is_reused(page, studio_url: str) -> None:
    pid = _project(studio_url, "E2E rebuild profile")
    r = httpx.post(f"{studio_url}/api/v2/projects/{pid}/jobs", headers=H, json={
        "title": "Rock", "category_id": "props", "idempotency_key": f"idem-{pid}", "source": "manual",
        "candidate_count": 1, "items": [{"name": "Rock", "brief": "a mossy rock, moody light"}]})
    assert r.status_code == 201, r.text
    page.goto(f"/p/{pid}/jobs/{r.json()['job']['id']}")
    page.get_by_role("button", name="Run · enhance prompt").click()
    page.get_by_role("button", name=re.compile(r"^Confirm prompt")).click(timeout=15000)
    expect(page.get_by_role("button", name=re.compile(r"^candidate 1 of round 1"))).to_be_visible(timeout=20000)
    expect(page.get_by_text("QA pending")).to_have_count(0, timeout=20000)
    page.keyboard.press("1")
    if page.get_by_role("dialog").is_visible():
        page.get_by_role("button", name="Approve anyway").click()
    expect(page.get_by_role("button", name="Approved ✓ · click to undo")).to_be_visible(timeout=10000)
    page.get_by_role("button", name=re.compile(r"^Generate 3D →")).click()
    expect(page).to_have_url(re.compile(r"/build$"))
    page.get_by_role("button", name=re.compile(r"^Change settings, rebuild")).click(timeout=30000)
    reuse = page.get_by_label("rebuild reuse")
    expect(reuse).to_have_count(0)
    page.get_by_role("button", name=re.compile("Build profile overrides")).click()
    page.get_by_label("alpha_mode").select_option("blend")
    expect(reuse).to_have_text("Reuses: cut-out, sample, bake — only the material stage runs")
    page.get_by_label("fill_holes").select_option("disabled")
    expect(reuse).to_have_text("Reuses: cut-out, sample — re-exports on the 3D worker")
    page.get_by_label("fill_holes").select_option("")
    shot(page, "rebuild_profile_overrides")
