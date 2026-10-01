"""Moved to assetstudio_node.engines.fake; shim until direct mode is removed (WP2.10)."""
from __future__ import annotations

from assetstudio_node.engines.fake import (
    _FAKE_VARIANTS,
    FakeAux,
    FakeEngine,
    FakeWorker3d,
    _edit_png,
    _FakeLease,
    _png,
)

__all__ = [
    "_edit_png",
    "_FAKE_VARIANTS",
    "FakeAux",
    "FakeEngine",
    "_FakeLease",
    "FakeWorker3d",
    "_png",
]
