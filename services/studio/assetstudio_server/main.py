"""Studio API: the product API and sole owner of production state. ComfyUI/aux are internal adapters."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from . import errors
from .coordinator.runner import Coordinator
from .journal import IdempotencyConflict
from .routers import (
    batches,
    batches_v2,
    jobs,
    library,
    media,
    projects,
    runner_api,
    runners,
    variant_plans,
    variants,
)
from .runner_errors import RunnerError
from .services.runner_maintenance import RunnerMaintenance
from .settings import Settings
from .studio import Studio, build_studio

CSRF_HEADER = "x-assetstudio"
RUNNER_PREFIX = "/api/runner/"  # bearer-authenticated; browsers never hold runner tokens, so no CSRF gate


def create_app(settings: Settings | None = None, studio: Studio | None = None) -> FastAPI:
    settings = settings or (studio.settings if studio else Settings())
    st = studio or build_studio(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        coord, maintenance = None, None
        if settings.start_coordinator:
            coord = Coordinator(st)
            coord.start()
            maintenance = RunnerMaintenance(st)
            maintenance.start()
        app.state.coordinator = coord
        yield
        if maintenance is not None:
            maintenance.stop()
        if coord is not None:
            coord.stop()
        st.close()

    app = FastAPI(title="AssetStudio", version="0.2.0", lifespan=lifespan)
    app.state.studio = st
    app.state.coordinator = None
    errors.install(app)

    @app.exception_handler(IdempotencyConflict)
    async def _idem(_: Request, e: IdempotencyConflict) -> JSONResponse:
        return JSONResponse(errors.body(e.code, str(e)), status_code=409)

    @app.exception_handler(RunnerError)
    async def _runner_error(_: Request, e: RunnerError) -> JSONResponse:
        return JSONResponse(e.body(), status_code=e.status)

    browser_validation = app.exception_handlers[RequestValidationError]

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, e: RequestValidationError) -> Response:
        if not request.url.path.startswith(RUNNER_PREFIX):
            return await browser_validation(request, e)
        errs = [{"path": ".".join(str(p) for p in err["loc"]), "message": err["msg"]} for err in e.errors()]
        return JSONResponse(RunnerError(400, "invalid_input", "request failed validation", {"errors": errs}).body(),
                            status_code=400)

    @app.middleware("http")
    async def same_origin(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """State changes need a custom header (forces a CORS preflight, which we never grant cross-origin)."""
        path = request.url.path
        if request.method not in ("GET", "HEAD", "OPTIONS") and path.startswith("/api/") \
                and not path.startswith(RUNNER_PREFIX):
            if request.headers.get(CSRF_HEADER) != "1":
                return JSONResponse(errors.body("csrf", f"missing {CSRF_HEADER} header"), status_code=403)
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
                return JSONResponse(errors.body("csrf", "cross-origin request refused"), status_code=403)
        return await call_next(request)

    app.include_router(projects.router)
    app.include_router(library.router)
    app.include_router(media.router)
    app.include_router(batches.router)
    app.include_router(projects.v2)
    app.include_router(jobs.router)
    app.include_router(batches_v2.router)
    app.include_router(variants.router)
    app.include_router(variant_plans.router)
    app.include_router(runner_api.router)
    app.include_router(runners.router)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "simulated": st.simulated, "instance_id": settings.instance_id}

    web = settings.web_dir
    if web.is_dir():
        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> Response:
            if path.startswith("api/"):
                return JSONResponse(errors.body("not_found", "unknown API route"), status_code=404)
            candidate = (web / path).resolve()
            if path and candidate.is_file() and candidate.is_relative_to(web.resolve()):
                return FileResponse(candidate)
            return FileResponse(Path(web) / "index.html", headers={"Cache-Control": "no-store"})
    return app


def run() -> None:
    import os

    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host=os.environ.get("STUDIO_HOST", "127.0.0.1"),
                port=int(os.environ.get("STUDIO_PORT", "8190")), log_level="info",
                timeout_graceful_shutdown=3)  # open SSE streams must not block shutdown
