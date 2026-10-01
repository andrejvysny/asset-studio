"""Protocol version negotiation."""
from __future__ import annotations

from collections.abc import Iterable

PROTOCOL_VERSION = 1
SUPPORTED_VERSIONS = frozenset({1})


def negotiate(offered: Iterable[int]) -> int | None:
    common = SUPPORTED_VERSIONS.intersection(offered)
    return max(common) if common else None
