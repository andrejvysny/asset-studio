"""Request body cap. Declared and streamed sizes are both enforced; upload routes register larger limits."""
from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .errors import respond

DEFAULT_MAX = 65536


class _TooLarge(BaseException):  # noqa: N818 - BaseException so the app's `except Exception` handlers cannot turn it into a 503
    pass


class BodyLimit:
    def __init__(self, app: ASGIApp, default_max: int = DEFAULT_MAX, overrides: dict[str, int] | None = None) -> None:
        self.app, self.default_max = app, default_max
        self.overrides: dict[str, int] = overrides if overrides is not None else {}

    def limit_for(self, path: str) -> int:
        for suffix, limit in self.overrides.items():
            if path.endswith(suffix):
                return limit
        return self.default_max

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope["path"])
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            await self._reject(scope, receive, send, limit)
            return
        seen = 0
        started = False

        async def counted() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise _TooLarge
            return message

        async def tracked(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counted, tracked)
        except _TooLarge:
            if not started:
                await self._reject(scope, receive, send, limit)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        response = respond(413, "resource_limit", f"request body exceeds {limit} bytes",
                           details={"limit": "request_body_bytes", "max": limit})
        await response(scope, receive, send)
