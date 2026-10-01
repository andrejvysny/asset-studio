"""Authenticated read API: model3d assets, exact versions, descriptors, deliveries, verified artifact content.

Only `assets:read` is needed. GET routes never write; `POST /resolve` is the one place deliveries get prepared.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any

from assetstudio_core.canonical_v1 import canonical_bytes
from assetstudio_core.delivery import AssetRef, Representation
from assetstudio_core.domain import AssetManifest
from assetstudio_core.ids import is_id
from assetstudio_core.inheritance import descendants
from assetstudio_storage import delivery as store_delivery
from assetstudio_storage.local import LocalBackend
from assetstudio_storage.project import manifest_key
from assetstudio_storage.repo import IntegrityError, StorageError
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from ..registry import ProjectContext
from ..services import deliveries as svc
from .auth import principal
from .errors import IntegrationError
from .principal import Principal, require

log = logging.getLogger("assetstudio.integration")
router = APIRouter(prefix="/api/integration/v1/libraries/{library_id}")
KIND = "model3d"


def _not_found(code: str = "asset_not_found") -> IntegrationError:
    return IntegrationError(404, code, "not found")


def _ctx(request: Request, who: Principal, library_id: str) -> ProjectContext:
    require(who, "assets:read", library_id)
    try:
        return request.app.state.studio.registry.get(library_id)
    except Exception:
        log.warning("library %s unavailable", library_id, exc_info=True)
        raise IntegrationError(503, "temporarily_unavailable", "library temporarily unavailable",
                               retryable=True) from None


def _ids(asset_id: str, version_id: str | None = None) -> None:
    if not is_id(asset_id, "ast"):
        raise _not_found()
    if version_id is not None and not is_id(version_id, "ver"):
        raise _not_found("version_unavailable")


def _load(ctx: ProjectContext, asset_id: str, version_id: str) -> Any:
    try:
        return svc.load_version(ctx.store, asset_id, version_id)
    except svc.LookupFailed as e:
        raise _not_found(e.code) from None


def _limits(request: Request) -> dict[str, Any]:
    return request.app.state.contracts.capabilities["limits"]


# --- listing ---------------------------------------------------------------------------------------------------
def _qhash(q: str | None, category: str | None, tags: list[str], kind: str) -> str:
    return hashlib.sha256(canonical_bytes([q, category, sorted(tags), kind])).hexdigest()[:16]


def _decode_cursor(cursor: str | None, qhash: str, rev: int | None) -> int | None:
    """Offset to continue from, or None when the cursor no longer matches the query or the index
    (`rev` None: check the query only)."""
    if not cursor:
        return 0
    try:
        c = json.loads(base64.urlsafe_b64decode(cursor.encode() + b"=" * (-len(cursor) % 4)))
        offset = int(c["o"])
    except (ValueError, KeyError, TypeError):
        return None
    return offset if c.get("h") == qhash and (rev is None or c.get("r") == rev) and offset >= 0 else None


def _encode_cursor(offset: int, qhash: str, rev: int) -> str:
    raw = canonical_bytes({"o": offset, "h": qhash, "r": rev})
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@router.get("/assets")
def list_assets(library_id: str, request: Request, who: Principal = Depends(principal), q: str | None = None,
                category: str | None = None, tags: str | None = None, kind: str = KIND, cursor: str | None = None,
                limit: int = Query(60, ge=1, le=200)) -> dict[str, Any]:
    ctx = _ctx(request, who, library_id)
    if kind != KIND:
        raise IntegrationError(422, "unsupported_representation", f"only {KIND} assets are served")
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
    qhash = _qhash(q, category, tag_list, kind)
    offset = _decode_cursor(cursor, qhash, None)
    if offset is None:
        return {"items": [], "next_cursor": None, "reset_required": True}
    cats = descendants(ctx.config()[0], category) if category else None
    # Rows and revision come from one index snapshot: a page is never labelled with a revision it was not read at.
    rows, total, rev = ctx.index.query_at_revision(categories=cats, kind=KIND, q=q, tags=tag_list, limit=limit,
                                                   offset=offset)
    if cursor and _decode_cursor(cursor, qhash, rev) is None:
        return {"items": [], "next_cursor": None, "reset_required": True}
    nxt = offset + len(rows)
    return {"items": [_item(ctx, r) for r in rows],
            "next_cursor": _encode_cursor(nxt, qhash, rev) if nxt < total else None}


def _item(ctx: ProjectContext, row: dict[str, Any]) -> dict[str, Any]:
    manifest = ctx.store.get(manifest_key(row['asset_id']), AssetManifest)[0]
    return {"asset_id": row["asset_id"], "display_name": row["display_name"], "category_id": row["category_id"],
            "tags": row["tags"], "current_version_id": row["current_version_id"],
            "display_version": row["display_version"], "metadata_revision": manifest.revision,
            "has_thumbnail": bool(row["preview_artifact_id"])}


@router.get("/assets/{asset_id}")
def asset_detail(library_id: str, asset_id: str, request: Request,
                 who: Principal = Depends(principal)) -> dict[str, Any]:
    ctx = _ctx(request, who, library_id)
    _ids(asset_id)
    try:
        manifest = ctx.store.get(manifest_key(asset_id), AssetManifest)[0]
    except Exception:
        raise _not_found() from None
    if manifest.kind.value != KIND:
        raise _not_found()
    return {"library_id": library_id, "asset_id": asset_id, "display_name": manifest.display_name,
            "category_id": manifest.category_id, "tags": manifest.tags, "metadata_revision": manifest.revision,
            "current_version_id": manifest.current_version_id,
            "versions": [{"version_id": v.version_id, "display_version": v.display_version,
                          "published_at": v.published_at} for v in manifest.versions]}


# --- exact version ------------------------------------------------------------------------------------------------
@router.get("/assets/{asset_id}/versions/{version_id}")
def version_detail(library_id: str, asset_id: str, version_id: str, request: Request,
                   who: Principal = Depends(principal)) -> dict[str, Any]:
    ctx = _ctx(request, who, library_id)
    _ids(asset_id, version_id)
    manifest, version = _load(ctx, asset_id, version_id)
    ref = AssetRef(server_id=request.app.state.identity.server_id, library_id=library_id, asset_id=asset_id,
                   version_id=version_id)
    found = store_delivery.descriptor(ctx.store, asset_id, version_id)
    descriptor = ({"state": "ready", "sha256": found[0].descriptor_sha256, "json": found[1].decode("utf-8")}
                  if found else {"state": "not_prepared", "sha256": None, "json": None})
    vref = manifest.version(version_id)
    return {"asset_ref": ref.model_dump(), "asset_key": ref.key(), "display_version": version.display_version,
            "published_at": vref.published_at if vref else None,
            "is_current": manifest.current_version_id == version_id, "descriptor": descriptor,
            "deliveries": [svc.summary(d) for d in store_delivery.deliveries(ctx.store, asset_id, version_id)],
            "source_available": "godot_source" in version.artifacts, "licence": version.licence,
            "has_thumbnail": "preview" in version.artifacts}


def _immutable_json(raw: bytes, sha: str) -> Response:
    return Response(raw, media_type="application/json",
                    headers={"ETag": f'"{sha}"', "X-Content-SHA256": sha,
                             "Cache-Control": "private, max-age=31536000, immutable",
                             "X-Content-Type-Options": "nosniff"})


@router.get("/assets/{asset_id}/versions/{version_id}/descriptor")
def descriptor_bytes(library_id: str, asset_id: str, version_id: str, request: Request,
                     who: Principal = Depends(principal)) -> Response:
    ctx = _ctx(request, who, library_id)
    _ids(asset_id, version_id)
    _load(ctx, asset_id, version_id)
    found = store_delivery.descriptor(ctx.store, asset_id, version_id)
    if found is None:
        raise IntegrationError(409, "delivery_preparing", "descriptor not prepared yet", retryable=True,
                               details={"hint": "POST resolve"})
    return _immutable_json(found[1], found[0].descriptor_sha256)


def _file_response(ctx: ProjectContext, artifact_id: str) -> Response:
    art = ctx.store.artifact(artifact_id)
    headers = {"Cache-Control": "private, max-age=31536000, immutable", "X-Content-SHA256": art.sha256,
               "ETag": f'"{art.sha256}"', "X-Content-Type-Options": "nosniff"}
    backend = ctx.store.repo
    backend.verify_blob(art.sha256, art.size)  # corrupt bytes are never served
    if isinstance(backend, LocalBackend) and (path := backend.blob_path(art.sha256)) is not None:
        return FileResponse(path, media_type=art.mime, headers=headers)  # Range -> 206
    return Response(backend.read_blob_verified(art.sha256), media_type=art.mime, headers=headers)


@router.get("/assets/{asset_id}/versions/{version_id}/thumbnail")
def thumbnail(library_id: str, asset_id: str, version_id: str, request: Request,
              who: Principal = Depends(principal)) -> Response:
    ctx = _ctx(request, who, library_id)
    _ids(asset_id, version_id)
    _, version = _load(ctx, asset_id, version_id)
    preview = version.artifacts.get("preview")
    if preview is None or not str(preview.get("mime", "")).startswith("image/"):
        raise _not_found()
    return _file_response(ctx, preview["artifact_id"])


# --- resolve ------------------------------------------------------------------------------------------------------
class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    representations: list[Representation] = Field(default=["portable_glb_v1"], min_length=1, max_length=3)


class ResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refs: list[AssetRef] = Field(min_length=1, max_length=200)
    target: Target = Target()


def _entry(ref: AssetRef, state: str, code: str | None = None, message: str = "",
           result: svc.EnsureResult | None = None, reps: list[str] | None = None,
           representations: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    found = result.descriptor if result and state == "ready" else None
    status = {"state": state, "error": {"code": code, "message": message} if code else None}
    return {"asset_ref": ref.model_dump(), "asset_key": ref.key(), **status,
            "descriptor_sha256": found[0].descriptor_sha256 if found else None,
            "descriptor_json": found[1].decode("utf-8") if found else None,
            "deliveries": [svc.summary(d) for d in (result.deliveries if result and state == "ready" else [])
                           if d.representation in (reps or [])],
            "dependencies": [],
            "representations": representations if representations is not None else {r: status for r in reps or []}}


def _statuses(res: svc.EnsureResult, reps: list[str]) -> dict[str, tuple[str, str | None, str]]:
    """Per requested representation: (state, error code, message)."""
    ready = {d.representation for d in res.deliveries} if res.descriptor is not None else set()
    out: dict[str, tuple[str, str | None, str]] = {}
    for rep in reps:
        if rep in ready:
            out[rep] = ("ready", None, "")
        elif rep in res.issues:
            out[rep] = res.issues[rep]
        elif res.state == "temporarily_unavailable":
            out[rep] = (res.state, "temporarily_unavailable", res.reason or "")
        else:
            out[rep] = ("unsupported", "unsupported_representation", res.reason or "representation not available")
    return out


def _from_result(ref: AssetRef, res: svc.EnsureResult, reps: list[str]) -> dict[str, Any]:
    statuses = _statuses(res, reps)
    detail = {r: {"state": s, "error": {"code": c, "message": m} if c else None} for r, (s, c, m) in statuses.items()}
    if any(s == "ready" for s, _, _ in statuses.values()):
        return _entry(ref, "ready", result=res, reps=reps, representations=detail)
    state, code, message = next(iter(statuses.values()))
    return _entry(ref, state, code, message, representations=detail)


def _resolve_one(request: Request, ctx: ProjectContext, ref: AssetRef, reps: list[str]) -> dict[str, Any]:
    app = request.app
    if ref.server_id != app.state.identity.server_id:
        return _entry(ref, "server_identity_mismatch", "server_identity_mismatch", "reference names another server",
                      reps=reps)
    if ref.library_id != ctx.id:
        return _entry(ref, "forbidden", "forbidden", "reference names another library", reps=reps)
    try:
        res = svc.ensure_version(app.state.studio, ctx, ref.server_id, ref.asset_id, ref.version_id, _limits(request),
                                 representations=reps)
    except svc.LookupFailed as e:
        return _entry(ref, "not_found", e.code, "asset or version not found", reps=reps)
    except IntegrityError as e:
        return _entry(ref, "temporarily_unavailable", "integrity_mismatch", str(e)[:200], reps=reps)
    except StorageError as e:
        return _entry(ref, "temporarily_unavailable", "temporarily_unavailable", str(e)[:200], reps=reps)
    return _from_result(ref, res, reps)


@router.post("/resolve")
def resolve(library_id: str, body: ResolveBody, request: Request, who: Principal = Depends(principal)
            ) -> dict[str, Any]:
    ctx = _ctx(request, who, library_id)
    reps = list(body.target.representations)
    return {"entries": [_resolve_one(request, ctx, ref, reps) for ref in body.refs]}


# --- deliveries and artifact content -----------------------------------------------------------------------------
@router.get("/deliveries/{delivery_id}/manifest")
def delivery_manifest(library_id: str, delivery_id: str, request: Request,
                      who: Principal = Depends(principal)) -> Response:
    ctx = _ctx(request, who, library_id)
    rec = store_delivery.delivery_by_id(ctx.store, delivery_id) if is_id(delivery_id, "dlv") else None
    if rec is None:
        raise _not_found()
    return _immutable_json(store_delivery.manifest_bytes(ctx.store, rec), rec.manifest_sha256)


@router.get("/artifacts/{artifact_id}/content")
def artifact_content(library_id: str, artifact_id: str, request: Request,
                     who: Principal = Depends(principal)) -> Response:
    ctx = _ctx(request, who, library_id)
    if not is_id(artifact_id, "art") or store_delivery.artifact_marker(ctx.store, artifact_id) is None:
        raise _not_found()
    return _file_response(ctx, artifact_id)
