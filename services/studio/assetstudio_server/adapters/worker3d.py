"""Moved to assetstudio_node.engines.worker3d; shim until direct mode is removed (WP2.10)."""
from __future__ import annotations

from assetstudio_node.engines.worker3d import (
    MAX_WAIT_S,
    POLL_S,
    TIMEOUT,
    Worker3dClient,
)

__all__ = [
    "MAX_WAIT_S",
    "POLL_S",
    "TIMEOUT",
    "Worker3dClient",
]
