"""Push dispatch listener: POST /v1/offers, signature verified against the agent's current Studio keys."""
from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

from assetstudio_client import keys
from assetstudio_protocol.execution import Offer
from pydantic import ValidationError

if TYPE_CHECKING:
    from .agent import RunnerAgent

log = logging.getLogger("assetstudio_node.push")
MAX_BODY = 1024 * 1024


def _make_handler(agent: RunnerAgent) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, text: str = "") -> None:
            body = text.encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != "/v1/offers":
                return self._reply(404, "not found")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._reply(411, "Content-Length required")
            if length < 0 or length > MAX_BODY:
                return self._reply(413, "body too large")
            body = self.rfile.read(length)
            try:
                offer = Offer.model_validate_json(body)
            except ValidationError:
                return self._reply(400, "invalid offer")
            if not keys.verify_offer(offer, agent.studio_keys):
                log.warning("rejected pushed offer %s: bad signature", offer.attempt_id)
                return self._reply(403, "invalid signature")
            self._reply(202 if agent.enqueue_offer(offer) else 200)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


class PushListener:
    def __init__(self, listen: str, agent: RunnerAgent) -> None:
        host, _, port = listen.rpartition(":")
        self._server = ThreadingHTTPServer((host, int(port)), _make_handler(agent))
        self._thread = threading.Thread(target=self._server.serve_forever, name="push-listener", daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
