"""Runner-protocol error: rendered flat as ErrorBody (docs/modular/compute-runner.md R12/R17), unlike ApiError."""
from __future__ import annotations

from typing import Any

from assetstudio_protocol.errors import ErrorBody, ErrorCode


class RunnerError(Exception):
    def __init__(self, status: int, code: ErrorCode, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail or {}

    def body(self) -> dict[str, Any]:
        return ErrorBody(code=self.code, message=self.message[:500], detail=self.detail).model_dump()
