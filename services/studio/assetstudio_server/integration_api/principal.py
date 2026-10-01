"""Authenticated caller and the scope/library check every route goes through (defined below the transport layer)."""
from __future__ import annotations

from ..services.principals import Principal, require

__all__ = ["Principal", "require"]
