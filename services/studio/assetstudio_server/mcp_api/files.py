"""Moving bytes for remote agents: an upload spool (inline base64 or signed PUT URL) and signed artifact downloads.

Signed URLs are stateless HMAC tokens (per-process key, 15 min). An upload token is single-use: its spool file is
created exclusively. Downloads stream straight from the Studio artifact route (sha-verified, Range support).
Studio never fetches a URL on an agent's behalf.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

TTL_S = 15 * 60
SPOOL_KEEP_S = 24 * 3600  # unconsumed uploads are swept after a day
UPLOAD_ID = re.compile(r"upl_[0-9a-f]{24}")
SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


@dataclass
class Upload:
    upload_id: str
    filename: str
    size: int
    sha256: str

    def view(self) -> dict[str, Any]:
        return {"upload_id": self.upload_id, "filename": self.filename, "size": self.size, "sha256": self.sha256}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


class _ArtifactProxy(Response):
    """Serves the Studio artifact route as-is (streaming, Range, sha headers) instead of buffering it here."""

    def __init__(self, app: ASGIApp, project_id: str, artifact_id: str) -> None:
        super().__init__()
        self.app, self.path = app, f"/api/v1/projects/{project_id}/artifacts/{artifact_id}/content"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        keep = [(k, v) for k, v in scope["headers"] if k in (b"range", b"if-range")]
        inner = {**scope, "path": self.path, "raw_path": self.path.encode(), "root_path": "",
                 "query_string": b"download=true", "headers": [(b"host", b"studio"), *keep]}
        await self.app(inner, receive, send)


class FileSpool:
    def __init__(self, root: Path, base_url: str, app: ASGIApp, max_inline: int, max_upload: int) -> None:
        self.root, self.base_url, self.app = root, base_url, app
        self.max_inline, self.max_upload = max_inline, max_upload
        self._key = secrets.token_bytes(32)

    # --- signed tokens ---------------------------------------------------------------------------------------
    def _sign(self, claims: dict[str, Any]) -> str:
        body = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).rstrip(b"=")
        mac = base64.urlsafe_b64encode(hmac.digest(self._key, body, "sha256")).rstrip(b"=")
        return f"{body.decode()}.{mac.decode()}"

    def _verify(self, token: str, kind: str) -> dict[str, Any] | None:
        body, _, mac = token.partition(".")
        want = base64.urlsafe_b64encode(hmac.digest(self._key, body.encode(), "sha256")).rstrip(b"=").decode()
        if not mac or not hmac.compare_digest(mac, want):
            return None
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if claims.get("k") != kind or claims.get("exp", 0) < time.time():
            return None
        return claims

    # --- spool -----------------------------------------------------------------------------------------------
    def _paths(self, upload_id: str) -> tuple[Path, Path]:
        if not UPLOAD_ID.fullmatch(upload_id):
            raise ToolError(f"invalid upload_id {upload_id!r}")
        return self.root / upload_id, self.root / f"{upload_id}.json"

    def _sweep(self) -> None:
        cutoff = time.time() - SPOOL_KEEP_S
        for p in self.root.glob("upl_*"):
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)

    def _new(self, filename: str) -> tuple[str, str]:
        self.root.mkdir(parents=True, exist_ok=True)
        self._sweep()
        name = SAFE_NAME.sub("_", Path(filename).name)[:200] or "upload"
        return f"upl_{secrets.token_hex(12)}", name

    def _finish(self, upload_id: str, name: str, size: int, digest: str) -> Upload:
        up = Upload(upload_id, name, size, digest)
        self._paths(upload_id)[1].write_text(json.dumps(up.view()))
        return up

    def put_inline(self, filename: str, data_base64: str) -> Upload:
        if len(data_base64) > self.max_inline * 4 // 3 + 4:
            raise ToolError(f"too_large: inline uploads are limited to {self.max_inline} bytes; use create_upload_url")
        try:
            data = base64.b64decode(data_base64, validate=True)
        except ValueError as e:
            raise ToolError("invalid base64 data") from e
        upload_id, name = self._new(filename)
        path, _ = self._paths(upload_id)
        path.write_bytes(data)
        return self._finish(upload_id, name, len(data), hashlib.sha256(data).hexdigest())

    def upload_url(self, filename: str) -> dict[str, Any]:
        upload_id, name = self._new(filename)
        exp = int(time.time()) + TTL_S
        token = self._sign({"k": "up", "id": upload_id, "name": name, "exp": exp})
        return {"upload_id": upload_id, "url": f"{self.base_url}/files/up/{token}", "method": "PUT",
                "max_bytes": self.max_upload, "expires_at": exp,
                "note": "PUT the raw bytes (no multipart, no auth header); the URL works once"}

    def read(self, upload_id: str, max_bytes: int | None = None) -> tuple[str, bytes]:
        """Bytes of a completed upload. Uploads stay until swept, so a retried tool call can reuse them."""
        path, meta = self._paths(upload_id)
        if not meta.exists():
            raise ToolError(f"unknown or incomplete upload {upload_id!r}")
        if max_bytes is not None and path.stat().st_size > max_bytes:
            raise ToolError(f"too_large: this tool accepts at most {max_bytes} bytes")
        return json.loads(meta.read_text())["filename"], path.read_bytes()

    def download_url(self, project_id: str, artifact_id: str) -> dict[str, Any]:
        exp = int(time.time()) + TTL_S
        token = self._sign({"k": "down", "p": project_id, "a": artifact_id, "exp": exp})
        return {"url": f"{self.base_url}/files/down/{token}", "method": "GET", "expires_at": exp}

    # --- HTTP routes (outside bearer auth: the signed token is the proof) ------------------------------------
    async def _put(self, request: Request) -> Response:
        claims = self._verify(request.path_params["token"], "up")
        if claims is None:
            return _error(403, "invalid_token", "upload URL is invalid or expired")
        path, meta = self._paths(claims["id"])
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return _error(409, "used", "this upload URL was already used")
        size, digest = 0, hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > self.max_upload:
                        raise OverflowError
                    digest.update(chunk)
                    f.write(chunk)
        except OverflowError:
            path.write_bytes(b"")  # keep the file: the token stays used
            return _error(413, "too_large", f"uploads are limited to {self.max_upload} bytes")
        return JSONResponse(self._finish(claims["id"], claims["name"], size, digest.hexdigest()).view())

    async def _get(self, request: Request) -> Response:
        claims = self._verify(request.path_params["token"], "down")
        if claims is None:
            return _error(403, "invalid_token", "download URL is invalid or expired")
        return _ArtifactProxy(self.app, claims["p"], claims["a"])

    def register_routes(self, mcp: FastMCP, prefix: str) -> None:
        mcp.custom_route(f"{prefix}up/{{token}}", methods=["PUT"], include_in_schema=False)(self._put)
        mcp.custom_route(f"{prefix}down/{{token}}", methods=["GET"], include_in_schema=False)(self._get)
