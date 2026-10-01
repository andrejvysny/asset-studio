"""Static-source publication: `publications:preview` (streamed bounded upload), `publications:commit`, and the
`publication-operations/{key}` lookup. Business logic lives in services/source_publications.py."""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile

from ..lifecycle import GateClosed, MutationGate
from ..registry import ProjectContext
from ..services import source_publications as sp
from ..services.principals import ServiceError
from .admission import Admission
from .auth import principal
from .errors import IntegrationError
from .principal import Principal, require

log = logging.getLogger("assetstudio.integration")
router = APIRouter(prefix="/api/integration/v1/libraries/{library_id}")
CHUNK = 1 << 20
SMALL_CAPS = {"descriptor": 1 << 20, "report": 4 << 20, "thumbnail": 8 << 20}
BIG_PARTS = ("source", "portable")
ALLOWED_PARTS = (*BIG_PARTS, *SMALL_CAPS)


def _ctx(request: Request, who: Principal, library_id: str) -> ProjectContext:
    require(who, "assets:publish", library_id)
    try:
        return request.app.state.studio.registry.get(library_id)
    except Exception:
        log.warning("library %s unavailable", library_id, exc_info=True)
        raise IntegrationError(503, "temporarily_unavailable", "library temporarily unavailable",
                               retryable=True) from None


def _limits(request: Request) -> dict[str, Any]:
    return request.app.state.contracts.capabilities["limits"]


def _copy_capped(source: BinaryIO, dest: Path, cap: int, name: str) -> sp.StagedPart:
    digest, size = hashlib.sha256(), 0
    with dest.open("wb") as out:
        while chunk := source.read(CHUNK):
            size += len(chunk)
            if size > cap:
                raise IntegrationError(413, "resource_limit", f"part {name!r} exceeds {cap} bytes",
                                       details={"limit": f"part_{name}_bytes", "max": cap})
            digest.update(chunk)
            out.write(chunk)
    return sp.StagedPart(dest, digest.hexdigest(), size)


def _stage_parts(form: FormData, parts_dir: Path, big_cap: int) -> dict[str, sp.StagedPart]:
    items = form.multi_items()
    names = [name for name, _ in items]
    if len(set(names)) != len(names) or not set(names) <= set(ALLOWED_PARTS):
        raise IntegrationError(400, "invalid_request", "unknown or repeated multipart part",
                               details={"allowed": list(ALLOWED_PARTS)})
    staged: dict[str, sp.StagedPart] = {}
    for name, upload in items:
        if not isinstance(upload, UploadFile):
            raise IntegrationError(400, "invalid_request", f"part {name!r} must be a file")
        staged[name] = _copy_capped(upload.file, parts_dir / name, big_cap if name in BIG_PARTS else SMALL_CAPS[name],
                                    name)
    return staged


def _tracked[T](gate: MutationGate, fn: Callable[..., T], *args: Any) -> T:
    """Runs in the worker thread: the gate counts the thread itself, not the (cancellable) request task."""
    try:
        with gate.enter():
            return fn(*args)
    except GateClosed:
        raise ServiceError("temporarily_unavailable", "server is shutting down; retry", retryable=True) from None


@router.post("/publications:preview")
async def preview_upload(library_id: str, request: Request, who: Principal = Depends(principal)) -> dict[str, Any]:
    ctx = _ctx(request, who, library_id)
    ctx.require_writable()
    app = request.app
    adm: Admission = app.state.admission
    settings, limits = app.state.studio.settings, _limits(request)
    big_cap = limits["publication_upload_max_bytes"]
    adm.check_preview_quota(who.credential_id)
    declared = request.headers.get("content-length", "")
    # Upload (declared size) plus the source tree that validation expands next to it.
    need = (int(declared) if declared.isdigit() else big_cap) + limits["source_expanded_max_bytes"]
    with adm.reserve(need):
        preview_id, parts_dir = sp.new_preview(settings)
        try:
            with adm.hold(preview_id):
                async with request.form(max_files=len(ALLOWED_PARTS), max_fields=0) as form:
                    # I/O-bound and bounded by BodyLimit: stays on the default pool, not the processing slots.
                    staged = await run_in_threadpool(_stage_parts, form, parts_dir, big_cap)
                return await adm.run(_tracked, app.state.studio.mutations, sp.preview, app.state.studio, ctx, who,
                                     app.state.identity.server_id, staged, app.state.contracts.capabilities,
                                     adm.active_snapshot())
        except BaseException:
            sp.discard(settings, preview_id)
            raise


def publisher(library_id: str, who: Principal = Depends(principal)) -> Principal:
    """Scope check as a dependency so it runs before body validation: a read-only token always gets 403."""
    require(who, "assets:publish", library_id)
    return who


@router.post("/publications:commit")
async def commit_publication(library_id: str, body: sp.CommitPublication, request: Request,
                             who: Principal = Depends(publisher)) -> dict[str, Any]:
    ctx = _ctx(request, who, library_id)
    app = request.app
    with app.state.admission.hold(body.preview_id):
        return await app.state.admission.run(_tracked, app.state.studio.mutations, sp.commit, app.state.studio, ctx,
                                             who, app.state.identity.server_id, body, _limits(request))


@router.get("/publication-operations/{key}")
def publication_operation(library_id: str, key: str, request: Request,
                          who: Principal = Depends(principal)) -> dict[str, Any]:
    ctx = _ctx(request, who, library_id)
    if not 8 <= len(key) <= 100:
        raise IntegrationError(400, "invalid_request", "idempotency key must be 8..100 characters")
    return sp.operation(ctx, key)
