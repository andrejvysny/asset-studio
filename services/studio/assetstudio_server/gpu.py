"""Moved to assetstudio_node.admission; shim until direct mode is removed (WP2.10)."""
from __future__ import annotations

from assetstudio_node.admission import (
    GpuLane,
    LaneWorker,
    OwnershipUnknown,
    _check_release,
    nvidia_smi,
)

__all__ = [
    "_check_release",
    "GpuLane",
    "LaneWorker",
    "nvidia_smi",
    "OwnershipUnknown",
]
