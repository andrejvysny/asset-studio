"""Integration change feed: cursors, restart epochs, per-token filtering, long-poll, reset."""
from __future__ import annotations

import base64
import json
import threading
import time
from typing import Any

from assetstudio_server.events import EventBus

from tests.conftest import Api, new_project
from tests.contract.test_api_library import _glb, _import
from tests.integration_support import client, integration_app_for, make_token

CHANGES = "/api/integration/v1/changes"


def _cursor(epoch: str, seq: int) -> str:
    raw = json.dumps({"e": epoch, "s": seq}).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _poll(c: Any, cursor: str | None = None, timeout_s: float = 0) -> dict[str, Any]:
    params: dict[str, Any] = {"timeout_s": timeout_s}
    if cursor is not None:
        params["cursor"] = cursor
    r = c.get(CHANGES, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _setup(api: Api, libraries: list[str] | None = None) -> tuple[Any, str, str]:
    app, _ = integration_app_for(api)
    lib = new_project(api)
    token = make_token(app, "godot", ["assets:read"], libraries if libraries is not None else [lib])
    return client(app, token), lib, token


def test_no_cursor_returns_current_cursor_then_empty(api: Api) -> None:
    c, _, _ = _setup(api)
    first = _poll(c)
    assert first["events"] == [] and first["reset_required"] is False
    again = _poll(c, first["cursor"])
    assert again == {"cursor": first["cursor"], "events": [], "reset_required": False}


def test_import_patch_and_rollback_emit_asset_hints(api: Api) -> None:
    c, lib, _ = _setup(api)
    cur = _poll(c)["cursor"]
    imp = _import(api, lib, "crate", _glb(), "crate.glb")
    aid, v1 = imp["asset_id"], imp["version_id"]
    got = _poll(c, cur)
    assert {"type": "asset_current_changed", "library_id": lib, "asset_id": aid} in got["events"]
    assert all(set(e) <= {"type", "library_id", "asset_id"} for e in got["events"])

    detail = api.get(f"/api/v1/projects/{lib}/assets/{aid}")
    r = api.raw("PATCH", f"/api/v1/projects/{lib}/assets/{aid}",
                json={"expected_revision": detail["manifest"]["revision"], "tags": ["a"]})
    assert r.status_code == 200, r.text
    meta = _poll(c, got["cursor"])
    assert meta["events"] == [{"type": "asset_metadata_changed", "library_id": lib, "asset_id": aid}]

    v2 = _import(api, lib, "crate", _glb(), "crate.glb", target_asset_id=aid, expected_current_version=v1, idempotency_key="imp-v2-xxxx")
    assert v2["version_id"] != v1
    out = _poll(c, meta["cursor"])
    assert out["events"] and out["events"][0]["type"] == "asset_current_changed"
    api.post(f"/api/v1/projects/{lib}/assets/{aid}:set-current", {
        "version_id": v1, "expected_current_version": v2["version_id"], "idempotency_key": "rollback-0001"})
    back = _poll(c, out["cursor"])
    assert back["events"] == [{"type": "asset_current_changed", "library_id": lib, "asset_id": aid}]


def test_ungranted_library_events_hidden_but_cursor_advances(api: Api) -> None:
    app, _ = integration_app_for(api)
    a, b = new_project(api, "A"), new_project(api, "B")
    c = client(app, make_token(app, "bonly", ["assets:read"], [b]))
    cur = _poll(c)["cursor"]
    _import(api, a, "crate", _glb(), "crate.glb")
    got = _poll(c, cur)
    assert got["events"] == [] and got["cursor"] != cur and got["reset_required"] is False
    assert _poll(c, got["cursor"])["events"] == []


def test_reset_and_invalid_cursors(api: Api) -> None:
    c, lib, _ = _setup(api)
    bus = api.studio.events
    foreign = _poll(c, _cursor("other-epoch", 0))
    assert foreign["reset_required"] is True and foreign["events"] == []
    future = _poll(c, _cursor(bus.epoch, bus.seq + 5))
    assert future["reset_required"] is True
    api.studio.events = EventBus(maxlen=3)
    old = _poll(c)["cursor"]
    for _ in range(6):
        api.studio.events.publish("library", project_id=lib)
    expired = _poll(c, old)
    assert expired["reset_required"] is True and expired["events"] == []
    assert _poll(c, expired["cursor"])["reset_required"] is False
    for bad in ("!!!", base64.urlsafe_b64encode(b"[1]").decode(), _cursor("e", -1)):
        r = c.get(CHANGES, params={"cursor": bad, "timeout_s": 0})
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"
    assert c.get(CHANGES, params={"timeout_s": 21}).status_code == 400


def test_overflow_during_active_long_poll_requires_reset(api: Api) -> None:
    """H08: the ring wraps after the route's initial cursor check; the poll must not skip the lost events."""
    c, lib, _ = _setup(api)
    api.studio.events = bus = EventBus(maxlen=2)
    cur = _poll(c)["cursor"]

    def burst() -> None:
        with bus._cond:  # one atomic burst: no collection can observe a partial ring
            for i in range(4):
                bus.publish("library", project_id=lib, asset_id=f"ast_{i}", change="published")

    threading.Timer(0.3, burst).start()
    got = _poll(c, cur, timeout_s=5)
    assert got["reset_required"] is True and got["events"] == []
    assert _poll(c, got["cursor"])["reset_required"] is False


def test_epochs_are_unique_per_bus() -> None:
    a, b = EventBus(), EventBus()
    assert a.epoch != b.epoch and ":" not in a.epoch


def test_non_library_and_family_events_projection(api: Api) -> None:
    c, lib, _ = _setup(api)
    cur = _poll(c)["cursor"]
    bus = api.studio.events
    bus.publish("job", project_id=lib, job_id="x")
    bus.publish("library", project_id=lib)
    assert _poll(c, cur)["events"] == [{"type": "library_changed", "library_id": lib}]


def test_long_poll_returns_early_on_event(api: Api) -> None:
    c, lib, _ = _setup(api)
    cur = _poll(c)["cursor"]
    threading.Timer(0.3, lambda: api.studio.events.publish(
        "library", project_id=lib, asset_id="ast_x", change="metadata")).start()
    t0 = time.monotonic()
    got = _poll(c, cur, timeout_s=5)
    assert time.monotonic() - t0 < 2.5
    assert got["events"] == [{"type": "asset_metadata_changed", "library_id": lib, "asset_id": "ast_x"}]


def test_requires_a_scope_and_auth(api: Api) -> None:
    app, _ = integration_app_for(api)
    assert client(app).get(CHANGES).status_code == 401
