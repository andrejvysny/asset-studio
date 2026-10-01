"""Health, capabilities and library listing."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

from ..registry import Registry
from .auth import principal
from .errors import IntegrationError
from .principal import Principal

log = logging.getLogger("assetstudio.integration")
router = APIRouter(prefix="/api/integration/v1")
SCOPES = ("assets:read", "assets:publish")


@router.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "assetstudio-integration", "api_version": 1}


@router.get("/capabilities")
def capabilities(request: Request, who: Principal = Depends(principal)) -> dict[str, Any]:
    caps = request.app.state.contracts.capabilities
    return {
        "server_id": request.app.state.identity.server_id,
        "api_version": caps["api_version"],
        "contract_version": caps["contract_version"],
        "source_package_version": caps["source_package_version"],
        "representations": caps["representations"],
        "known_capabilities": caps["known_capabilities"],
        "limits": caps["limits"],
        "granted": {"library_ids": sorted(who.library_ids), "scopes": sorted(who.scopes)},
    }


def _library(registry: Registry, library_id: str, scopes: list[str]) -> dict[str, Any]:
    try:
        ctx = registry.get(library_id)
        return {"library_id": library_id, "name": ctx.name, "scopes": scopes, "state": "available"}
    except Exception:  # one failing library must not blank the others
        log.warning("library %s unavailable", library_id, exc_info=True)
        return {"library_id": library_id, "name": None, "scopes": scopes, "state": "unavailable"}


@router.get("/libraries")
def libraries(request: Request, who: Principal = Depends(principal)) -> dict[str, Any]:
    if not who.scopes & set(SCOPES):
        raise IntegrationError(403, "forbidden", "token does not grant this access")
    registry: Registry = request.app.state.studio.registry
    scopes = sorted(who.scopes)
    return {"libraries": [_library(registry, lid, scopes) for lid in sorted(who.library_ids)]}
