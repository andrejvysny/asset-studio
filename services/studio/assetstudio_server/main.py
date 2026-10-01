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


log = logging.getLogger("assetstudio")


def create_app(settings: Settings | None = None, studio: Studio | None = None, *, owns_studio: bool = True) -> FastAPI:
    """owns_studio=False: the caller closes the Studio after ALL listeners (companions share it) have stopped."""
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
        if owns_studio:
            if not st.mutations.close_and_wait(30):
                log.error("integration mutations still running after 30s; closing Studio anyway")
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


async def _guarded(name: str, server: uvicorn.Server, everyone: list[uvicorn.Server], failures: list[str]) -> None:
    """A listener that fails to start/serve takes the whole process down; any listener ending stops the others."""
    try:
        await server.serve()
        # uvicorn returns quietly when startup fails without an exit request of its own (e.g. lifespan error)
        if not server.started and not any(o.should_exit for o in everyone if o is not server):
            raise RuntimeError("listener did not start")
    except (SystemExit, Exception) as e:  # uvicorn bind failure raises SystemExit
        if not any(o.should_exit for o in everyone if o is not server):
            log.error("%s listener failed (%s: %s); stopping Studio", name, type(e).__name__, e)
            failures.append(name)
    finally:
        for o in everyone:
            o.should_exit = True


def _close_studio(st: Studio, timeout: float = 60) -> None:
    if not st.mutations.close_and_wait(timeout):
        log.error("integration mutations still running after %ss; closing Studio anyway", timeout)
    st.close()


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    app = create_app(settings, owns_studio=False)
    st: Studio = app.state.studio
    failures: list[str] = []
    try:
        _serve_all(app, st, settings, failures)
    finally:
        _close_studio(st)  # every listener has stopped (or never started): the only close
    if failures:
        raise SystemExit(1)


def _serve_all(app: FastAPI, st: Studio, settings: Settings, failures: list[str]) -> None:
    companions: list[uvicorn.Server] = []
    names: list[str] = []
    if settings.mcp_enabled:
        from .mcp_api.server import build_mcp_app

        mcp_app = build_mcp_app(app, st, settings)
        names.append("mcp")
        companions.append(_CompanionServer(uvicorn.Config(
            mcp_app, host=settings.mcp_host, port=settings.mcp_port, log_level="info", timeout_graceful_shutdown=3)))
    if settings.integration_enabled:
        from .integration_api.app import bind_problem, build_integration_app, log_bind

        problem = bind_problem(settings)
        if problem:
            logging.getLogger("assetstudio.integration").error(problem)
            raise SystemExit(2)
        log_bind(settings)
        names.append("integration")
        companions.append(_CompanionServer(uvicorn.Config(
            build_integration_app(st, settings), host=settings.integration_host,
            port=settings.integration_port, log_level="info", timeout_graceful_shutdown=3,
            ssl_certfile=settings.integration_tls_cert or None, ssl_keyfile=settings.integration_tls_key or None)))
    main = _MainServer(uvicorn.Config(app, host=os.environ.get("STUDIO_HOST", "127.0.0.1"),
                                      port=int(os.environ.get("STUDIO_PORT", "8190")), log_level="info",
                                      timeout_graceful_shutdown=3),  # open SSE streams must not block shutdown
                       companions)

    everyone = [main, *companions]

    async def serve() -> None:
        await asyncio.gather(_guarded("main", main, everyone, failures),
                             *(_guarded(n, c, everyone, failures) for n, c in zip(names, companions, strict=False)))
    asyncio.run(serve())
