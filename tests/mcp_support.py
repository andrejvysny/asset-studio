"""MCP test harness: the real listener stack (bearer auth -> Streamable HTTP -> tools -> REST loopback), in-process."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from assetstudio_server.mcp_api.server import McpApp, build_mcp_app
from assetstudio_server.studio import Studio
from fastapi import FastAPI
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

MCP_URL = "http://127.0.0.1:8191/mcp"


def mcp_app_for(app: FastAPI) -> McpApp:
    studio: Studio = app.state.studio
    return build_mcp_app(app, studio, studio.settings)


def http_client(mcp_app: McpApp, token: str | None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=mcp_app), base_url="http://127.0.0.1:8191",
                             headers=headers, timeout=60)


@asynccontextmanager
async def mcp_session(mcp_app: McpApp, token: str) -> AsyncIterator[ClientSession]:
    async with mcp_app.mcp.session_manager.run(), http_client(mcp_app, token) as http:
        async with streamable_http_client(MCP_URL, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def call(session: ClientSession, tool: str, **args: Any) -> Any:
    """Tool result as JSON; raises AssertionError with the tool's error text when it failed."""
    res = await session.call_tool(tool, args)
    text = res.content[0].text if res.content else ""
    if res.isError:
        raise AssertionError(text)
    return json.loads(text) if text else None


async def call_error(session: ClientSession, tool: str, **args: Any) -> str:
    res = await session.call_tool(tool, args)
    assert res.isError, f"{tool} unexpectedly succeeded"
    return res.content[0].text
