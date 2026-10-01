"""Integration listener app: FastAPI (only /api/integration/v1) wrapped by BodyLimit, then IntegrationAuth outermost."""
from __future__ import annotations

import logging

from fastapi import APIRouter, FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from ..services import source_publications
from ..settings import Settings
from ..studio import Studio
from . import errors, routes_assets, routes_changes, routes_meta, routes_publish
from .admission import Admission
from .auth import IntegrationAuth
from .capabilities import load_contracts
from .identity import ServerIdentity, load_or_create
from .limits import BodyLimit
from .tokens import IntegrationTokenStore

log = logging.getLogger("assetstudio.integration")
LOOPBACK = ("127.0.0.1", "::1", "localhost")
# Later modules (assets, changes, publish) append their routers here before the app is built.
ROUTERS: list[APIRouter] = [routes_meta.router, routes_changes.router, routes_assets.router,
                            routes_publish.router]


def token_store(settings: Settings) -> IntegrationTokenStore:
    return IntegrationTokenStore(settings.integration_dir / "tokens.json")


def bind_problem(settings: Settings) -> str | None:
    """Why the configured bind must be refused, or None. Loopback is always fine."""
    if settings.integration_container_bind or settings.integration_host in LOOPBACK:
        return None
    if settings.integration_tls_cert and settings.integration_tls_key:
        return None
    if settings.integration_allow_insecure_lan:
        return None
    return (f"refusing to bind the integration API to {settings.integration_host!r}: set STUDIO_INTEGRATION_TLS_CERT "
            "and STUDIO_INTEGRATION_TLS_KEY, or STUDIO_INTEGRATION_ALLOW_INSECURE_LAN=1 for cleartext LAN development")


def log_bind(settings: Settings) -> None:
    if settings.integration_container_bind:
        log.info("integration API in container: exposure governed by compose INTEGRATION_BIND")
    elif settings.integration_host not in LOOPBACK and not (
            settings.integration_tls_cert and settings.integration_tls_key):
        log.warning("cleartext integration API on LAN: tokens can be intercepted")


class IntegrationApp:
    """The listener's ASGI app. `fastapi` and `body_limit` stay reachable for later routers/upload limits."""

    def __init__(self, asgi: ASGIApp, fastapi: FastAPI, body_limit: BodyLimit) -> None:
        self.asgi, self.fastapi, self.body_limit = asgi, fastapi, body_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.asgi(scope, receive, send)


def build_integration_app(studio: Studio, settings: Settings, *, tokens: IntegrationTokenStore | None = None,
                          identity: ServerIdentity | None = None) -> IntegrationApp:
    app = FastAPI(title="AssetStudio integration API", docs_url=None, redoc_url=None,
                  openapi_url="/api/integration/v1/openapi.json")
    app.state.studio, app.state.settings = studio, settings
    app.state.tokens = tokens or token_store(settings)
    app.state.identity = identity or load_or_create(settings.integration_dir / "server.json")
    app.state.contracts = load_contracts(settings.contracts_dir)
    app.state.admission = Admission(settings)
    errors.install(app)
    for router in ROUTERS:
        app.include_router(router)
    limit = BodyLimit(app)
    limit.overrides["publications:preview"] = app.state.contracts.capabilities["limits"]["publication_upload_max_bytes"]
    try:
        source_publications.sweep_expired(settings)
    except OSError:
        log.warning("expired publication previews could not be swept", exc_info=True)
    return IntegrationApp(IntegrationAuth(limit, app.state.tokens), app, limit)
