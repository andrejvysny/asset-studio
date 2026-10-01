"""Bearer authentication for the integration listener. Runs before any request body is read."""
from __future__ import annotations

from fastapi import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from .errors import IntegrationError, respond
from .principal import Principal
from .tokens import IntegrationTokenStore

STATE_KEY = "integration_principal"
HEALTH_PATH = "/api/integration/v1/health"


class IntegrationAuth:
    """Every HTTP request needs a valid token, except exact `open_paths`."""

    def __init__(self, app: ASGIApp, store: IntegrationTokenStore,
                 open_paths: tuple[str, ...] = (HEALTH_PATH,)) -> None:
        self.app, self.store, self.open_paths = app, store, open_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.open_paths:
            await self.app(scope, receive, send)
            return
        header = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
        scheme, _, token = header.partition(" ")
        info = self.store.verify(token.strip()) if scheme.lower() == "bearer" else None
        if info is None:
            response = respond(401, "unauthorized", "valid bearer token required",
                               headers={"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})[STATE_KEY] = Principal(
            info.credential_id, info.name, frozenset(info.scopes), frozenset(info.library_ids))
        await self.app(scope, receive, send)


def principal(request: Request) -> Principal:
    found = request.scope.get("state", {}).get(STATE_KEY)
    if not isinstance(found, Principal):
        raise IntegrationError(401, "unauthorized", "valid bearer token required")
    return found
