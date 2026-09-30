"""Studio API: the product API and sole owner of production state. ComfyUI/aux are internal adapters."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import FrameType

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from . import actor, errors
from .coordinator.runner import Coordinator
from .journal import IdempotencyConflict
from .routers import batches, batches_v2, jobs, library, media, projects, variant_plans, variants
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
            coord = Coordinator(st)
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

    @app.middleware("http")
    async def acting_as(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """Agent identity from the in-process MCP loopback only (see actor.py); everyone else is the operator."""
        token = actor.set_current(actor.from_headers(request.headers.get(actor.ACTOR_HEADER),
                                                     request.headers.get(actor.INTERNAL_HEADER)))
        try:
            return await call_next(request)
        finally:
            actor.reset(token)

    app.include_router(projects.router)
    app.include_router(library.router)
    app.include_router(media.router)
    app.include_router(batches.router)
    app.include_router(projects.v2)
    app.include_router(jobs.router)
    app.include_router(batches_v2.router)
    app.include_router(variants.router)
    app.include_router(variant_plans.router)

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


class _MainServer(uvicorn.Server):
    """Owns signal handling and forwards shutdown to companion servers (which must not install handlers)."""

    def __init__(self, config: uvicorn.Config, companions: list[uvicorn.Server]) -> None:
        super().__init__(config)
        self.companions = companions

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        super().handle_exit(sig, frame)
        for c in self.companions:
            c.should_exit, c.force_exit = self.should_exit, self.force_exit


class _CompanionServer(uvicorn.Server):
    def capture_signals(self) -> contextlib.AbstractContextManager[None]:  # type: ignore[override]
        return contextlib.nullcontext()


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    app = create_app(settings)
    companions: list[uvicorn.Server] = []
    if settings.mcp_enabled:
        from .mcp_api.server import build_mcp_app

        mcp_app = build_mcp_app(app, app.state.studio, settings)
        companions.append(_CompanionServer(uvicorn.Config(
            mcp_app, host=settings.mcp_host, port=settings.mcp_port, log_level="info", timeout_graceful_shutdown=3)))
    main = _MainServer(uvicorn.Config(app, host=os.environ.get("STUDIO_HOST", "127.0.0.1"),
                                      port=int(os.environ.get("STUDIO_PORT", "8190")), log_level="info",
                                      timeout_graceful_shutdown=3),  # open SSE streams must not block shutdown
                       companions)

    async def serve() -> None:
        await asyncio.gather(main.serve(), *(c.serve() for c in companions))
    asyncio.run(serve())
