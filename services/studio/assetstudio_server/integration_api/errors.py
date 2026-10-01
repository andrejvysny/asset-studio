"""Integration API error body: {"error": {"code", "message", "retryable", "details"}} (contract error-codes.json)."""
from __future__ import annotations

import logging
from typing import Any

from assetstudio_storage.repo import Conflict, IntegrityError, NotFound
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..errors import ApiError
from ..services.principals import ServiceError

log = logging.getLogger("assetstudio.integration")


class IntegrationError(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False,
                 details: Any = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.retryable, self.details = status, code, message, retryable, details


def error_body(code: str, message: str, retryable: bool = False, details: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "retryable": retryable,
                      "details": {} if details is None else details}}


def respond(status: int, code: str, message: str, retryable: bool = False, details: Any = None,
            headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(error_body(code, message, retryable, details), status_code=status, headers=headers)


def _from_api_error(e: ApiError) -> JSONResponse:
    if e.code == "unknown_project":
        return respond(403, "forbidden", "token does not grant this access")
    if e.code == "read_only":
        return respond(503, "temporarily_unavailable", "library is temporarily read-only", retryable=True)
    return respond(409, e.code, e.message)


def _status(request: Request, e: ServiceError) -> int:
    if e.status is not None:
        return e.status
    codes = {c["code"]: c["http_status"] for c in request.app.state.contracts.error_codes["codes"]}
    return codes.get(e.code, 409)


def install(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def _service(request: Request, e: ServiceError) -> JSONResponse:
        return respond(_status(request, e), e.code, e.message, e.retryable, e.details)

    @app.exception_handler(IntegrationError)
    async def _integration(_: Request, e: IntegrationError) -> JSONResponse:
        return respond(e.status, e.code, e.message, e.retryable, e.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError) -> JSONResponse:
        errs = [{"path": ".".join(str(p) for p in err["loc"]), "message": err["msg"]} for err in e.errors()]
        return respond(400, "invalid_request", "request failed validation", details=errs)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, e: StarletteHTTPException) -> JSONResponse:
        if e.status_code == 404:
            return respond(404, "not_found", "unknown route")
        if e.status_code == 405:
            return respond(405, "invalid_request", "method not allowed")
        return respond(e.status_code, "invalid_request", "request rejected")

    @app.exception_handler(NotFound)
    async def _not_found(_: Request, e: NotFound) -> JSONResponse:
        return respond(404, "asset_not_found", "asset not found")

    @app.exception_handler(Conflict)
    async def _conflict(_: Request, e: Conflict) -> JSONResponse:
        return respond(409, getattr(e, "code", "conflict"), str(e))

    @app.exception_handler(IntegrityError)
    async def _integrity(_: Request, e: IntegrityError) -> JSONResponse:
        return respond(409, "integrity_mismatch", str(e))

    @app.exception_handler(ApiError)
    async def _api(_: Request, e: ApiError) -> JSONResponse:
        return _from_api_error(e)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, e: Exception) -> JSONResponse:
        log.error("unhandled integration error", exc_info=(type(e), e, e.__traceback__))  # never request headers
        return respond(503, "temporarily_unavailable", "internal error; retry later", retryable=True)
