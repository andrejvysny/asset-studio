"""What every tool module needs: the Studio app/container, token identity, and a loopback client bound to it."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from starlette.types import ASGIApp

from ..settings import Settings
from ..studio import Studio
from .auth import STATE_KEY, TokenInfo, TokenStore
from .client import StudioClient
from .files import FileSpool


@dataclass
class Deps:
    app: ASGIApp
    studio: Studio
    settings: Settings
    tokens: TokenStore
    files: FileSpool

    def identity(self, ctx: Context) -> TokenInfo:
        request = ctx.request_context.request
        info = request.scope.get("state", {}).get(STATE_KEY) if request is not None else None
        if info is None:
            raise ToolError("unauthenticated")
        return info

    def client(self, ctx: Context, write: bool = False) -> StudioClient:
        info = self.identity(ctx)
        if write and info.scope != "full":
            raise ToolError(f"forbidden: token {info.name!r} has scope 'read'; this tool changes state")
        return StudioClient(self.app, info.actor, info.scope)

    async def project_id(self, client: StudioClient, project_id: str | None) -> str:
        """Explicit id, or the only open project. Ambiguity is an error listing the choices."""
        if project_id:
            return project_id
        projects = (await client.get("/api/v1/projects"))["projects"]
        if len(projects) == 1:
            return str(projects[0]["id"])
        choices = ", ".join(f"{p['id']} ({p.get('name', '')})" for p in projects) or "none (create_project first)"
        raise ToolError(f"project_id required; projects: {choices}")


def idem(key: str | None) -> str:
    """Caller key (pass the same one when retrying) or a fresh one."""
    return key or f"mcp-{uuid.uuid4().hex}"


def compact(obj: Any, drop: tuple[str, ...]) -> Any:
    """Recursively drop bulky keys from a REST view for agent-sized output."""
    if isinstance(obj, dict):
        return {k: compact(v, drop) for k, v in obj.items() if k not in drop}
    if isinstance(obj, list):
        return [compact(v, drop) for v in obj]
    return obj
