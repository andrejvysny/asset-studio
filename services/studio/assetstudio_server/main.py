"""Studio API: the product API and sole owner of production state. ComfyUI/aux are internal adapters."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from . import errors
from .coordinator.handlers import HANDLERS
from .coordinator.runner import Coordinator
from .journal import IdempotencyConflict
from .routers import batches, library, projects
from .settings import Settings
from .studio import Studio, build_studio

CSRF_HEADER = "x-assetstudio"


def create_app(settings: Settings | None = None, studio: Studio | None = None) -> FastAPI:
    settings = settings or (studio.settings if studio else Settings())
    st = studio or build_studio(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        coord = None
        if settings.start_coordinator:
            coord = Coordinator(st, HANDLERS)
            coord.start()
        app.state.coordinator = coord
        yield
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

    @app.middleware("http")
    async def same_origin(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """State changes need a custom header (forces a CORS preflight, which we never grant cross-origin)."""
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
            if request.headers.get(CSRF_HEADER) != "1":
                return JSONResponse(errors.body("csrf", f"missing {CSRF_HEADER} header"), status_code=403)
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
                return JSONResponse(errors.body("csrf", "cross-origin request refused"), status_code=403)
        return await call_next(request)

    app.include_router(projects.router)
    app.include_router(library.router)
    app.include_router(batches.router)

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
