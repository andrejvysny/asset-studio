"""Style screen: multiple profiles, revision history + restore, planned-effects panel, and project reference-set
images in the Job references panel (SIMULATED engine). Setup that is not the subject goes through the API."""
from __future__ import annotations

import io
import re

import httpx
import pytest
from PIL import Image
from playwright.sync_api import expect

from tests.e2e.conftest import shot

pytestmark = pytest.mark.e2e
H = {"x-assetstudio": "1"}


def _project(url: str, name: str) -> str:
    return httpx.post(f"{url}/api/v1/projects", json={"name": name}, headers=H).json()["id"]


def _patch(url: str, pid: str, edit) -> dict:
    view = httpx.get(f"{url}/api/v1/projects/{pid}/config").json()
    cfg = view["config"]
    edit(cfg)
    r = httpx.patch(f"{url}/api/v1/projects/{pid}/config", json={"expected_revision": cfg["revision"], "config": cfg}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["config"]


def _no_errors(page) -> None:
    # The effects panel probes project defaults, which answers 422 `unresolved` until a kind is picked: expected.
    errors = [e for e in page.errors if "status of 422" not in e]  # type: ignore[attr-defined]
    assert not errors, errors


def _new_style(page, name: str) -> None:
    page.once("dialog", lambda d: d.accept(name))
    page.get_by_role("button", name="+ Style").click()
    expect(page.get_by_role("button", name=name, exact=True)).to_be_visible()


def _save(page) -> None:
    with page.expect_response(lambda r: r.request.method == "PATCH" and r.url.endswith("/config")) as done:
        page.get_by_role("button", name="Save style").click()
    assert done.value.ok, done.value.text()
    expect(page.get_by_role("button", name="Save style")).to_be_disabled(timeout=10000)


def test_two_styles_switch_edit_save(page, studio_url: str) -> None:
    pid = _project(studio_url, "E2E styles")
    page.goto(f"/p/{pid}/style")
    _new_style(page, "warm")
    page.get_by_label("style guide").fill("warm light")
    _new_style(page, "cold")
    page.get_by_label("style guide").fill("cold light")
    page.get_by_role("button", name="warm", exact=True).click()
    expect(page.get_by_label("style guide")).to_have_value("warm light")
    _save(page)
    styles = httpx.get(f"{studio_url}/api/v1/projects/{pid}/config").json()["config"]["styles"]
    assert {k: v["guide"] for k, v in styles.items()} == {"warm": "warm light", "cold": "cold light"}
    page.once("dialog", lambda d: d.accept("Bad Id"))
    page.get_by_role("button", name="+ Style").click()
    expect(page.get_by_text("is not a valid id")).to_be_visible()
    shot(page, "style_two_profiles")
    _no_errors(page)


def test_history_and_restore(page, studio_url: str) -> None:
    pid = _project(studio_url, "E2E style history")
    page.goto(f"/p/{pid}/style")
    _new_style(page, "hist")
    expect(page.get_by_text("save to start history")).to_be_visible()
    page.get_by_label("style guide").fill("first guide")
    _save(page)
    page.get_by_label("style guide").fill("second guide")
    _save(page)
    revs = page.get_by_label("style revisions").get_by_label(re.compile(r"^revision "))
    expect(revs).to_have_count(2, timeout=10000)
    expect(page.get_by_label("style revisions").get_by_text("current", exact=True)).to_have_count(1)
    page.get_by_role("button", name="Restore into draft").click()
    expect(page.get_by_label("style guide")).to_have_value("first guide")
    expect(page.get_by_role("button", name="Save style")).to_be_enabled()
    _save(page)
    expect(revs).to_have_count(2)
    expect(revs.filter(has_text="first guide").get_by_text("current", exact=True)).to_be_visible(timeout=10000)
    shot(page, "style_history")
    _no_errors(page)


def test_effects_panel_reports_inert_and_conditioning_fields(page, studio_url: str) -> None:
    pid = _project(studio_url, "E2E style effects")

    def edit(cfg: dict) -> None:
        cfg["styles"]["ink"] = {"label": "Ink", "guide": "bold ink lines", "negative": "", "palette": []}
        cfg["categories"] = [{"id": "concept", "slug": "concept", "label": "Concept", "defaults": {
            "kind": {"mode": "value", "value": "concept_art"}, "style": {"mode": "value", "value": "ink"},
            "build_profile": {"mode": "value", "value": "hero"}}}]

    _patch(studio_url, pid, edit)
    page.goto(f"/p/{pid}/style")
    page.get_by_label("effects category").select_option("concept")
    expect(page.get_by_label("effect build_profile unsupported")).to_be_visible(timeout=10000)
    expect(page.get_by_label("effect style.guide conditioning_only")).to_be_visible()
    expect(page.get_by_label("config warnings").get_by_text("build_profile")).to_be_visible()
    page.get_by_label("effects category").select_option("")
    expect(page.get_by_label("effects hint")).to_be_visible(timeout=10000)
    shot(page, "style_effects")
    _no_errors(page)


def test_job_shows_project_reference_set_images(page, studio_url: str) -> None:
    pid = _project(studio_url, "E2E set refs")
    buf = io.BytesIO()
    Image.new("RGB", (96, 64), (30, 160, 90)).save(buf, "PNG")
    up = httpx.post(f"{studio_url}/api/v1/projects/{pid}/references:upload", headers=H,
                    files={"file": ("moodboard.png", buf.getvalue(), "image/png")})
    assert up.status_code in (200, 201), up.text

    def edit(cfg: dict) -> None:
        cfg["reference_sets"]["mood"] = {"label": "Mood", "mode": "prompt_guidance", "images": [
            {"artifact_id": up.json()["artifact_id"], "label": "moodboard", "role": "", "source_rights": "own"}]}
        cfg["categories"] = [{"id": "concept", "slug": "concept", "label": "Concept", "defaults": {
            "kind": {"mode": "value", "value": "concept_art"}, "reference_set": {"mode": "value", "value": "mood"}}}]

    _patch(studio_url, pid, edit)
    r = httpx.post(f"{studio_url}/api/v2/projects/{pid}/jobs", headers=H, json={
        "title": "Tavern", "category_id": "concept", "idempotency_key": f"idem-{pid}", "source": "manual",
        "items": [{"name": "Tavern", "brief": "a tavern"}]})
    assert r.status_code in (200, 201), r.text
    page.goto(f"/p/{pid}/jobs/{r.json()['job']['id']}/prompt")
    expect(page.get_by_text("From project reference set")).to_be_visible(timeout=15000)
    expect(page.get_by_label("from project reference set").get_by_text("set · guidance")).to_be_visible()
    shot(page, "style_job_set_refs")
    _no_errors(page)
