"""Aggregate resource admission for publication: staging budget, disk floor, processing queue, preview quota, sweep."""
from __future__ import annotations

import shutil
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from assetstudio_server.integration_api.admission import Admission
from assetstudio_server.services import source_publications as sp

from tests.integration_publication_support import V1, PubEnv, glb_parts, make_env


@pytest.fixture
def env(make_api) -> PubEnv:
    return make_env(make_api)


def configure(env: PubEnv, **over: Any) -> Admission:
    """Swap in an Admission built from tightened settings (the Settings dataclass is shared, so copy it)."""
    adm = Admission(replace(env.api.studio.settings, **over))
    env.app.fastapi.state.admission = adm
    return adm


def error_of(env: PubEnv, parts: dict[str, bytes]) -> dict[str, Any]:
    body = env.preview(parts, expect=503)["error"]
    assert body["code"] == "temporarily_unavailable" and body["retryable"] is True
    return body


def test_staging_budget_refused_then_released(env: PubEnv) -> None:
    adm = configure(env, integration_staging_max_bytes=1 << 20)
    assert error_of(env, glb_parts())["details"]["reason"] == "staging_capacity"
    assert adm.reserved == 0 and env.previews_on_disk() == []


def test_disk_floor_breach(env: PubEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    adm = configure(env)
    real = shutil.disk_usage
    monkeypatch.setattr(shutil, "disk_usage", lambda p: real(p)._replace(free=adm.floor))
    assert error_of(env, glb_parts())["details"]["reason"] == "disk_floor"
    assert adm.reserved == 0


def test_reservation_released_after_success_and_failure(env: PubEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    adm = configure(env)
    env.preview(glb_parts())
    assert adm.reserved == 0 and adm.active_snapshot() == frozenset()

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(sp, "preview", boom)
    r = env.c.post(env.url("/publications:preview"), files={"portable": ("p.glb", b"x")})
    assert r.status_code >= 500
    assert adm.reserved == 0 and adm.active_snapshot() == frozenset()


def test_queue_full_answers_503_while_cheap_endpoints_stay_responsive(env: PubEnv,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    adm = configure(env, integration_processing_slots=1, integration_queue_max=0)
    started, release = threading.Event(), threading.Event()
    real = sp.preview

    def slow(*a: Any, **k: Any) -> Any:
        started.set()
        assert release.wait(30)
        return real(*a, **k)

    monkeypatch.setattr(sp, "preview", slow)
    first: dict[str, Any] = {}
    t = threading.Thread(target=lambda: first.update(r=env.c.post(env.url("/publications:preview"),
                                                                 files={"portable": ("p.glb", b"x")})))
    t.start()
    try:
        assert started.wait(30)
        assert error_of(env, glb_parts())["details"]["reason"] == "processing_queue_full"
        assert env.c.get(f"{V1}/health").status_code == 200
    finally:
        release.set()
        t.join(30)
    assert "r" in first and adm.reserved == 0


def test_per_credential_preview_cap(env: PubEnv) -> None:
    configure(env, integration_previews_per_token=2)
    env.preview(glb_parts())
    env.preview(glb_parts(2))
    assert error_of(env, glb_parts(3))["details"]["reason"] == "preview_quota"
    other = env.app.fastapi.state.tokens.create("other", ["assets:read", "assets:publish"], [env.lib])
    from tests.integration_support import client

    env.preview(glb_parts(3), c=client(env.app, other))


def test_sweep_skips_active_previews(env: PubEnv) -> None:
    receipt = env.preview(glb_parts())
    settings, pid = env.api.studio.settings, receipt["preview_id"]
    later = datetime.now(UTC) + timedelta(days=30)
    assert sp.sweep_expired(settings, later, frozenset({pid})) == 0
    assert pid in env.previews_on_disk()
    assert sp.sweep_expired(settings, later) == 1
    assert env.previews_on_disk() == []


def test_hold_tracks_active_previews(env: PubEnv) -> None:
    adm = configure(env)
    with adm.hold("ipv_a"):
        assert adm.active_snapshot() == {"ipv_a"}
    assert adm.active_snapshot() == frozenset()
