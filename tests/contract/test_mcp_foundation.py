"""MCP listener: bearer auth, token scopes, revocation, agent actor on decisions, orientation tools (SIMULATED)."""
from __future__ import annotations

import asyncio

from assetstudio_server import actor
from assetstudio_server.mcp_api.auth import TokenStore

from tests.conftest import Api, new_project
from tests.mcp_support import call, call_error, http_client, mcp_app_for, mcp_session


def test_requests_without_valid_token_are_refused(api: Api) -> None:
    app = mcp_app_for(api.c.app)

    async def body() -> None:
        async with app.mcp.session_manager.run():
            for token in (None, "ast_wrong", "not-a-token"):
                async with http_client(app, token) as http:
                    r = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
                    assert r.status_code == 401
                    assert r.headers["www-authenticate"] == "Bearer"
    asyncio.run(body())


def test_read_token_reads_but_cannot_mutate_and_revocation_is_immediate(api: Api) -> None:
    app = mcp_app_for(api.c.app)
    token = app.deps.tokens.create("observer", "read")

    async def body() -> None:
        async with mcp_session(app, token) as s:
            overview = await call(s, "studio_overview")
            assert overview["simulated"] is True and overview["projects"] == []
            assert "forbidden" in await call_error(s, "create_project", name="Nope")
            # the CLI writes the same file from another process: a second store instance simulates it
            assert TokenStore(app.deps.tokens.path).revoke("observer")
            async with http_client(app, token) as http:
                r = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
                assert r.status_code == 401
    asyncio.run(body())


def test_tokens_are_stored_hashed_with_private_permissions(api: Api) -> None:
    store = mcp_app_for(api.c.app).deps.tokens
    token = store.create("claude", "full")
    raw = store.path.read_text()
    assert token not in raw and "sha256" in raw
    assert store.path.stat().st_mode & 0o077 == 0
    assert [t["name"] for t in store.list()] == ["claude"] and "sha256" not in store.list()[0]
    assert store.verify(token).actor == "agent:claude"  # type: ignore[union-attr]


def test_full_token_creates_project_and_lists_tools(api: Api) -> None:
    app = mcp_app_for(api.c.app)
    token = app.deps.tokens.create("claude", "full")

    async def body() -> None:
        async with mcp_session(app, token) as s:
            names = {t.name for t in (await s.list_tools()).tools}
            assert {"studio_overview", "create_project", "project_summary", "list_recipes"} <= names
            created = await call(s, "create_project", name="Agent Project")
            summary = await call(s, "project_summary")  # the only project: id may be omitted
            assert summary["id"] == created["id"]
            recipes = await call(s, "list_recipes")
            assert any(r["id"] == "model3d.default" for r in recipes["recipes"])
            resources = {str(r.uri) for r in (await s.list_resources()).resources}
            assert {"assetstudio://guide/workflow", "assetstudio://schema/config"} <= resources
    asyncio.run(body())


def test_actor_header_needs_the_internal_secret(api: Api) -> None:
    assert actor.from_headers("agent:claude", actor.INTERNAL_SECRET) == "agent:claude"
    assert actor.from_headers("agent:claude", "guess") == actor.OPERATOR
    assert actor.from_headers("agent:claude", None) == actor.OPERATOR
    assert actor.from_headers("operator; drop", actor.INTERNAL_SECRET) == actor.OPERATOR
    pid = new_project(api)
    # a browser-style request claiming an agent identity is recorded as the operator
    r = api.raw("POST", f"/api/v2/projects/{pid}/jobs", headers={actor.ACTOR_HEADER: "agent:evil"},
                json={"title": "T", "kind": "concept_art", "items": [{"name": "A"}], "idempotency_key": "k" * 8})
    assert r.status_code in (200, 201), r.text


def test_verify_never_rewrites_the_token_file(api: Api) -> None:
    store = mcp_app_for(api.c.app).deps.tokens
    token = store.create("claude", "full")
    before = store.path.read_bytes()
    assert store.verify(token) is not None
    assert store.path.read_bytes() == before  # a stale rewrite could undo a concurrent CLI revoke
    assert store.list()[0]["last_used_at"] is not None
