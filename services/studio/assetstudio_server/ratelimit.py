"""Per-key token buckets for unauthenticated endpoints (R14). Memory is bounded by an LRU of buckets."""
from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from fastapi import Request

from .operator_auth import proxy_trusted
from .settings import Settings


class TokenBuckets:
    def __init__(self, per_min: int, burst: int, max_keys: int = 10_000,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._rate, self._burst, self._max, self._clock = max(per_min, 1) / 60.0, max(burst, 1), max_keys, clock
        self._buckets: OrderedDict[tuple[str, str], tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def take(self, key: tuple[str, str]) -> int:
        """0 when allowed, else whole seconds until a token is available."""
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.pop(key, (float(self._burst), now))
            tokens = min(float(self._burst), tokens + (now - last) * self._rate)
            allowed = tokens >= 1.0
            self._buckets[key] = (tokens - 1.0 if allowed else tokens, now)
            while len(self._buckets) > self._max:
                self._buckets.popitem(last=False)
        return 0 if allowed else max(1, math.ceil((1.0 - tokens) / self._rate))


def client_ip(request: Request, settings: Settings) -> str:
    """Socket peer, unless the proxy proved itself with the shared secret: then the LAST X-Forwarded-For hop, the
    address our proxy itself saw (earlier hops are client-supplied if a proxy appends instead of overwriting)."""
    peer = request.client.host if request.client else "unknown"
    if proxy_trusted(request, settings):
        last = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
        try:
            return str(ipaddress.ip_address(last))
        except ValueError:
            return peer
    return peer
