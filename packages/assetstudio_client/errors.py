"""Client exceptions. TransportError means the outcome is UNKNOWN (the request may or may not have been applied):
callers must treat it as uncertain, never as a refusal."""
from __future__ import annotations

from typing import Any

import httpx
from assetstudio_protocol.errors import ErrorBody
from pydantic import ValidationError


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.detail: dict[str, Any] = detail or {}

    @classmethod
    def from_response(cls, response: httpx.Response) -> ApiError:
        try:
            body = ErrorBody.model_validate_json(response.content)
        except (ValidationError, ValueError):
            return cls(response.status_code, "http_error", response.text[:300] or response.reason_phrase)
        return cls(response.status_code, body.code, body.message, body.detail)


class TransportError(Exception):
    """Network failure or timeout talking to Studio."""
