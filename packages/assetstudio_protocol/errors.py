"""Stable machine error codes (R12)."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import StringConstraints

from .base import Msg

ErrorCode = Literal[
    "invalid_input", "unsupported_contract", "protocol_incompatible", "unauthorized", "forbidden_scope",
    "stale_session", "stale_generation", "stale_revision", "node_unavailable", "uncertain_execution",
    "missing_artifact", "validation_failed", "resource_exhausted", "admission_rejected", "cancelled_by_operator",
]


class ErrorBody(Msg):
    code: ErrorCode
    message: Annotated[str, StringConstraints(max_length=500)]
    detail: dict[str, Any] = {}
