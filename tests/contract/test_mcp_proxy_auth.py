"""H15: in proxy auth mode the MCP loopback authorizes as an agent principal from the MCP token, not as owner."""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from assetstudio_server import actor
from assetstudio_server.main import create_app
from assetstudio_server.mcp_api.client import StudioClient, StudioError
from assetstudio_server.studio import build_studio
from fastapi.testclient import TestClient

from tests.conftest import HEADERS, make_settings
from tests.mcp_support import call, call_error, http_client, mcp_app_for, mcp_session

SECRET = "s3cret-proxy-value"
OWNER = {**HEADERS, "X-AssetStudio-Proxy-Secret": SECRET, "Remote-User": "olga", "Remote-Groups": "assetstudio-owners"}


@pytest.fixture
def proxy(tmp_path: Path) -> Iterator[TestClient]:
    settings = make_settings(tmp_path, coordinator=False)
    settings.auth_mode, settings.proxy_secret = "proxy", SECRET
    with TestClient(create_app(settings, build_studio(settings))) as c:
        yield c


def test_read_token_reads_and_mutations_are_forbidden(proxy: TestClient) -> None:
    app = mcp_app_for(proxy.app)  # type: ignore[arg-type]
    token = app.deps.tokens.create("observer", "read")

    async def body() -> None:
        async with mcp_session(app, token) as s:
            assert (await call(s, "studio_overview"))["projects"] == []
            assert "forbidden" in await call_error(s, "create_project", name="Nope")
        # the REST layer enforces it too, were a tool to forget its own scope check
        with pytest.raises(StudioError) as e:
            await StudioClient(app.deps.app, "agent:observer", "read").post("/api/v1/projects", {"name": "x"})
        assert e.value.status == 403
    asyncio.run(body())
    assert proxy.get("/api/v1/projects", headers=OWNER).json()["projects"] == []


def test_full_token_works_with_actor_and_admin_is_denied(proxy: TestClient) -> None:
    app = mcp_app_for(proxy.app)  # type: ignore[arg-type]
    token = app.deps.tokens.create("claude", "full")

    async def body() -> None:
        async with mcp_session(app, token) as s:
            pid = (await call(s, "create_project", name="Agent Project"))["id"]
            job = await call(s, "create_job", title="T", items=[{"name": "A", "kind": "concept_art"}], project_id=pid)
            assert "agent:claude" in str(job)
            full = StudioClient(app.deps.app, "agent:claude", "full")
            for method, path in (("POST", "/api/v1/attempts/a1:declare-lost"), ("POST", "/api/v1/runners/r1:revoke"),
                                 ("POST", "/api/v1/runtime/lanes/gpu:reset"), ("GET", "/api/v1/audit"),
                                 ("POST", "/api/v1/runner-groups"), ("POST", "/api/v1/projects:register"),
                                 ("PATCH", f"/api/v1/projects/{pid}/config")):
                with pytest.raises(StudioError) as e:
                    await full.call(method, path, json={})
                assert e.value.status == 403 and "operator administration" in str(e.value), (method, path)
    asyncio.run(body())


def test_forged_internal_headers_get_normal_proxy_auth(proxy: TestClient) -> None:
    forged = [
        {actor.ACTOR_HEADER: "agent:evil", actor.SCOPE_HEADER: "full"},  # no secret
        {actor.ACTOR_HEADER: "agent:evil", actor.SCOPE_HEADER: "full", actor.INTERNAL_HEADER: "guess"},
        {actor.ACTOR_HEADER: "agent:evil", actor.INTERNAL_HEADER: actor.INTERNAL_SECRET},  # no scope: partial
        {actor.ACTOR_HEADER: "agent:evil", actor.SCOPE_HEADER: "admin", actor.INTERNAL_HEADER: actor.INTERNAL_SECRET},
        {actor.ACTOR_HEADER: "operator", actor.SCOPE_HEADER: "full", actor.INTERNAL_HEADER: actor.INTERNAL_SECRET},
    ]
    for h in forged:
        assert proxy.get("/api/v1/projects", headers=h).status_code == 401, h
        assert proxy.post("/api/v1/projects", json={"name": "x"}, headers={**HEADERS, **h}).status_code == 401, h
    assert actor.internal_principal({}) is None


def test_revoked_token_never_reaches_the_loopback(proxy: TestClient) -> None:
    app = mcp_app_for(proxy.app)  # type: ignore[arg-type]
    token = app.deps.tokens.create("temp", "full")
    app.deps.tokens.revoke("temp")

    async def body() -> None:
        async with app.mcp.session_manager.run(), http_client(app, token) as http:
            body_ = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "create_project", "arguments": {"name": "Ghost"}}}
            r = await http.post("/mcp", json=body_)
            assert r.status_code == 401
    asyncio.run(body())
    assert proxy.get("/api/v1/projects", headers=OWNER).json()["projects"] == []


def test_loopback_without_internal_secret_is_401_in_proxy_mode(proxy: TestClient) -> None:
    async def body() -> int:
        t = httpx.ASGITransport(app=proxy.app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=t, base_url="http://studio") as c:
            return (await c.get("/api/v1/projects", headers={actor.ACTOR_HEADER: "agent:claude"})).status_code
    assert asyncio.run(body()) == 401
