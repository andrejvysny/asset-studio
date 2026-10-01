"""In-process loopback to the Studio REST API: MCP tools reuse its validation, errors, events and idempotency."""
from __future__ import annotations

import json
from typing import Any

import httpx
from mcp.server.fastmcp.exceptions import ToolError
from starlette.types import ASGIApp

from .. import actor as actor_mod
from ..main import CSRF_HEADER

DETAIL_MAX_CHARS = 4000


class StudioError(ToolError):
    """A REST error surfaced to the agent: `<status> <code>: <message>` plus the machine detail."""

    def __init__(self, status: int, code: str, message: str, detail: Any = None) -> None:
        text = f"{status} {code}: {message}"
        if detail is not None:
            text += "\ndetail: " + json.dumps(detail, default=str)[:DETAIL_MAX_CHARS]
        super().__init__(text)
        self.status, self.code, self.detail = status, code, detail


class StudioClient:
    def __init__(self, app: ASGIApp, actor: str, scope: str = "read") -> None:
        self.app, self.actor, self.scope = app, actor, scope  # default is least privilege

    def _headers(self) -> dict[str, str]:
        return {CSRF_HEADER: "1", actor_mod.ACTOR_HEADER: self.actor,
                actor_mod.INTERNAL_HEADER: actor_mod.INTERNAL_SECRET,
                actor_mod.SCOPE_HEADER: self.scope}

    async def _send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        if not path.startswith("/api/"):
            raise ToolError(f"refused: {path!r} is not a Studio API path")
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://studio", timeout=None) as c:
            r = await c.request(method, path, headers=self._headers(), **kw)
        if r.status_code >= 400:
            try:
                err = r.json().get("error") or {}
            except ValueError:
                err = {}
            raise StudioError(r.status_code, err.get("code", "http_error"), err.get("message", r.text[:500]),
                              err.get("detail"))
        return r

    async def call(self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None,
                   files: Any = None, data: dict[str, Any] | None = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        r = await self._send(method, path, json=json, params=clean, files=files, data=data)
        return r.json() if r.content else None

    async def get(self, path: str, **params: Any) -> Any:
        return await self.call("GET", path, params=params)

    async def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        return await self.call("POST", path, json=body, **kw)

    async def patch(self, path: str, body: Any) -> Any:
        return await self.call("PATCH", path, json=body)

    async def put(self, path: str, body: Any) -> Any:
        return await self.call("PUT", path, json=body)

    async def download(self, path: str) -> tuple[bytes, dict[str, str]]:
        r = await self._send("GET", path)
        return r.content, dict(r.headers)
