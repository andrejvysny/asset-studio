"""In-process change events with a resumable cursor. Invalidation hints only: clients re-read state via the API."""
from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from typing import Any

from assetstudio_core.canonical import now_iso


class EventBus:
    def __init__(self, maxlen: int = 5000) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._seq = 0
        self._cond = threading.Condition()
        # Unique per start (a restarted server invalidates old cursors); no ":" so "epoch:seq" SSE ids still parse.
        self.epoch = f"{time.time_ns():x}-{secrets.token_hex(4)}"

    def publish(self, type_: str, **fields: Any) -> None:
        with self._cond:
            self._seq += 1
            self._events.append({"seq": self._seq, "time": now_iso(), "type": type_, **fields})
            self._cond.notify_all()

    def since(self, cursor: int, timeout: float = 15.0) -> tuple[list[dict[str, Any]], bool]:
        """Events after cursor; second value True when the cursor is too old (client must re-read everything)."""
        with self._cond:
            if not any(e["seq"] > cursor for e in self._events) and cursor >= self._seq:
                self._cond.wait(timeout)
            oldest = self._events[0]["seq"] if self._events else self._seq + 1
            expired = cursor + 1 < oldest and cursor < self._seq
            return [e for e in self._events if e["seq"] > cursor], expired

    @property
    def seq(self) -> int:
        return self._seq
