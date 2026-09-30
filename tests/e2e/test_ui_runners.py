"""Runners panel on the Runtime screen: create group + token, see a registered runner, revoke it. SIMULATED engine."""
from __future__ import annotations

import re

import httpx
import pytest
from assetstudio_client import keys
from playwright.sync_api import expect

from tests.e2e.conftest import shot

pytestmark = pytest.mark.e2e
H = {"x-assetstudio": "1"}


def _register(studio_url: str, token: str, name: str) -> None:
    priv = keys.generate_private_key()
    r = httpx.post(f"{studio_url}/api/runner/v1/register", json={
        "schema": "assetstudio.runner.register.v1", "registration_token": token,
        "public_key": keys.public_key_b64(priv), "name": name,
        "platform": {"os": "linux", "arch": "x86_64", "hostname": "e2e-box"}})
    assert r.status_code == 200, r.text


def test_runners_token_register_revoke(page, studio_url: str) -> None:
    pid = httpx.post(f"{studio_url}/api/v1/projects", json={"name": "E2E runners"}, headers=H).json()["id"]
    page.goto(f"/p/{pid}/runtime")
    expect(page.get_by_text("No runners registered. Studio runs work locally (direct mode) until runners are added.")
           ).to_be_visible(timeout=10000)
    shot(page, "runners_empty")

    page.get_by_role("button", name="Add runner…").click()
    page.get_by_label("Group name").fill("e2e-group")
    page.get_by_role("button", name="Create registration token").click()
    box = page.get_by_test_id("registration-token")
    expect(box).to_be_visible(timeout=10000)
    token = box.inner_text().strip()
    assert re.fullmatch(r"[A-Za-z0-9_-]{20,}", token), token
    expect(page.get_by_text("assetstudio-node run --config runner.yaml")).to_be_visible()
    expect(page.get_by_text(re.compile(r"shown once"))).to_be_visible()
    shot(page, "runners_token")

    _register(studio_url, token, "e2e-runner")
    page.reload()
    card = page.get_by_test_id("runner-e2e-runner")
    expect(card).to_be_visible(timeout=10000)
    shot(page, "runners_card")

    page.once("dialog", lambda d: d.accept())
    card.get_by_role("button", name="Revoke").click()
    expect(card.get_by_text("revoked", exact=True)).to_be_visible(timeout=10000)
    shot(page, "runners_revoked")
    assert not [e for e in page.errors if "favicon" not in e], page.errors  # type: ignore[attr-defined]
    assert page.external == [], page.external  # type: ignore[attr-defined]
