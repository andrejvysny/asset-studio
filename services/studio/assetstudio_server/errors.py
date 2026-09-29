"""Stable machine-readable API errors. Never leak traces or secrets."""
from __future__ import annotations

import logging
from typing import Any

from assetstudio_core.ids import InvalidId
from assetstudio_core.review import ReviewError
from assetstudio_storage.project import InvalidProjectData
from assetstudio_storage.repo import Conflict, IntegrityError, NotFound, ReadOnly, StorageError
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger("assetstudio")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail


def body(code: str, message: str, detail: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "detail": detail}}


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(_: Request, e: ApiError) -> JSONResponse:
        return JSONResponse(body(e.code, e.message, e.detail), status_code=e.status)

    @app.exception_handler(ReviewError)
    async def _review(_: Request, e: ReviewError) -> JSONResponse:
        return JSONResponse(body(e.code, str(e), e.detail), status_code=e.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError) -> JSONResponse:
        errs = [{"path": ".".join(str(p) for p in err["loc"]), "message": err["msg"]} for err in e.errors()]
        return JSONResponse(body("invalid_request", "request failed validation", errs), status_code=400)

    @app.exception_handler(InvalidId)
    async def _bad_id(_: Request, e: InvalidId) -> JSONResponse:
        return JSONResponse(body("invalid_id", str(e)), status_code=400)

    @app.exception_handler(StorageError)
    async def _storage(_: Request, e: StorageError) -> JSONResponse:
        if isinstance(e, NotFound):
            return JSONResponse(body("not_found", str(e)), status_code=404)
        if isinstance(e, Conflict):
            return JSONResponse(body(getattr(e, "code", "conflict"), str(e) + "; reload and retry"), status_code=409)
        if isinstance(e, ReadOnly):
            return JSONResponse(body("read_only", str(e)), status_code=409)
        if isinstance(e, InvalidProjectData):
            return JSONResponse(body("invalid_project_data", str(e), e.errors), status_code=422)
        if isinstance(e, IntegrityError):
            return JSONResponse(body("integrity_error", str(e)), status_code=422)
        log.exception("storage error")
        return JSONResponse(body("storage_error", "storage operation failed"), status_code=503)
