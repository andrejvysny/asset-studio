"""Authenticated caller and the scope/library check every route goes through."""
from __future__ import annotations

from dataclasses import dataclass

from .errors import IntegrationError


@dataclass(frozen=True)
class Principal:
    token_name: str
    scopes: frozenset[str]
    library_ids: frozenset[str]

    @property
    def actor(self) -> str:
        return f"integration:{self.token_name}"


def require(principal: Principal, scope: str, library_id: str) -> None:
    """One error for a missing scope, an ungranted library and a nonexistent one: existence is never disclosed."""
    if scope not in principal.scopes or library_id not in principal.library_ids:
        raise IntegrationError(403, "forbidden", "token does not grant this access")
