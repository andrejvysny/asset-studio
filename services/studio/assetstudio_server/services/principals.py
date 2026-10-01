"""Transport-neutral caller identity and service failures for integration publication/delivery services.

Services raise `ServiceError` and receive a `Principal`; each listener (HTTP integration API today) authenticates and
translates. A principal's security identity is its immutable `credential_id`, never the reusable display name.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ServiceError(Exception):
    """Semantic failure with a contract error code (contracts/godot-integration/v1/error-codes.json).

    `status` is only set where the API historically answers with a status other than the code's contract status."""

    def __init__(self, code: str, message: str, *, retryable: bool = False, details: Any = None,
                 status: int | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.retryable, self.details, self.status = code, message, retryable, details, status


@dataclass(frozen=True)
class Principal:
    credential_id: str
    token_name: str
    scopes: frozenset[str]
    library_ids: frozenset[str]

    @property
    def actor(self) -> str:
        return f"integration:{self.token_name}"


def require(principal: Principal, scope: str, library_id: str) -> None:
    """One error for a missing scope, an ungranted library and a nonexistent one: existence is never disclosed."""
    if scope not in principal.scopes or library_id not in principal.library_ids:
        raise ServiceError("forbidden", "token does not grant this access")
