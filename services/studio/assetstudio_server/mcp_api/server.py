"""MCP endpoint for remote agents: Streamable HTTP on its own listener, bearer tokens, tools over the REST loopback."""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from ..settings import Settings
from ..studio import Studio
from . import guide
from .auth import BearerAuth, TokenStore
from .deps import Deps
from .files import FileSpool
from .tools import ALL_MODULES

log = logging.getLogger("assetstudio.mcp")
FILES_PREFIX = "/files/"
BODY_OVERHEAD = 1024 * 1024  # JSON-RPC envelope + other arguments around an inline base64 file


def token_store(settings: Settings) -> TokenStore:
    return TokenStore(settings.instance_dir / "mcp_tokens.json")


def _transport_security(settings: Settings) -> TransportSecuritySettings:
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    if settings.mcp_public_url:
        public = urlsplit(settings.mcp_public_url)
        hosts += [public.netloc, f"{public.hostname}:*"]
        origins.append(f"{public.scheme}://{public.netloc}")
    if settings.mcp_host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::"):
        hosts.append(f"{settings.mcp_host}:*")
    elif settings.mcp_host in ("0.0.0.0", "::") and not settings.mcp_public_url:
        log.info("MCP on all interfaces without STUDIO_MCP_PUBLIC_URL: only loopback Host headers are accepted")
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                     allowed_origins=origins)


def build_mcp(deps: Deps) -> FastMCP:
    inline_b64 = deps.settings.mcp_max_inline_bytes * 4 // 3 + 4
    mcp = FastMCP("AssetStudio", instructions=guide.INSTRUCTIONS, stateless_http=True,
                  max_request_body_size=inline_b64 + BODY_OVERHEAD,
                  transport_security=_transport_security(deps.settings))
    for module in ALL_MODULES:
        module.register(mcp, deps)
    guide.register(mcp, deps)
    return mcp


class McpApp:
    """The listener's ASGI app. Keeps `mcp`/`deps` reachable (tests drive `mcp.session_manager` directly)."""

    def __init__(self, asgi: ASGIApp, mcp: FastMCP, deps: Deps) -> None:
        self.asgi, self.mcp, self.deps = asgi, mcp, deps

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.asgi(scope, receive, send)


def build_mcp_app(app: ASGIApp, studio: Studio, settings: Settings, tokens: TokenStore | None = None) -> McpApp:
    files = FileSpool(settings.instance_dir / "mcp-spool", settings.mcp_base_url, app,
                      max_inline=settings.mcp_max_inline_bytes, max_upload=settings.max_upload_bytes)
    deps = Deps(app=app, studio=studio, settings=settings, tokens=tokens or token_store(settings), files=files)
    mcp = build_mcp(deps)
    files.register_routes(mcp, FILES_PREFIX)
    inner = mcp.streamable_http_app()
    return McpApp(BearerAuth(inner, deps.tokens, open_prefixes=(FILES_PREFIX,)), mcp, deps)
