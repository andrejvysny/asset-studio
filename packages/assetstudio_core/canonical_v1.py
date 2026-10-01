"""Godot integration contract v1 encodings (INT-SPEC-1.0 §4.4): canonical document bytes, decimal strings, asset keys.

Clients hash the raw bytes the server stored; nothing here is meant to be re-derived from a decoded dictionary.
"""
from __future__ import annotations

import hashlib
import math
import re
import struct
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any, Literal

from .canonical import canonical_json

DECIMAL_PLACES = 6  # micrometres for metre values; enough for placement metadata, stable across languages
DECIMAL_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]*[1-9])?$")
_QUANTUM = Decimal(1).scaleb(-DECIMAL_PLACES)
_ROUNDING = {"nearest": ROUND_HALF_EVEN, "floor": ROUND_FLOOR, "ceil": ROUND_CEILING}
Rounding = Literal["nearest", "floor", "ceil"]


class EncodingError(ValueError):
    pass


def _check(obj: Any, path: str) -> None:
    if isinstance(obj, float):
        raise EncodingError(f"{path}: non-integral scalars must be decimal strings")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise EncodingError(f"{path}: object keys must be strings")
            _check(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _check(v, f"{path}[{i}]")
    elif obj is not None and not isinstance(obj, (str, int, bool)):
        raise EncodingError(f"{path}: unsupported value type {type(obj).__name__}")


def canonical_bytes(obj: Any) -> bytes:
    """Sorted keys, no insignificant whitespace, UTF-8, no floats/NaN. The only writer of shared contract bytes."""
    _check(obj, "$")
    return canonical_json(obj)


def decimal_str(value: float | int | Decimal | str, rounding: Rounding = "nearest") -> str:
    """Finite value -> canonical decimal string (max 6 places, no exponent, no trailing zeros, "-0" -> "0").

    Use "floor" for lower bounds and "ceil" for upper bounds so the quantized box still encloses the geometry.
    """
    if isinstance(value, float) and not math.isfinite(value):
        raise EncodingError(f"non-finite value {value!r}")
    try:
        d = Decimal(value) if not isinstance(value, Decimal) else value
        q = d.quantize(_QUANTUM, rounding=_ROUNDING[rounding])
    except (InvalidOperation, ValueError) as e:
        raise EncodingError(f"invalid decimal {value!r}") from e
    if not q.is_finite():
        raise EncodingError(f"non-finite value {value!r}")
    text = format(q, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    assert DECIMAL_RE.fullmatch(text), text
    return text


def parse_decimal(text: str) -> float:
    if not isinstance(text, str) or not DECIMAL_RE.fullmatch(text) or text == "-0":
        raise EncodingError(f"not a canonical decimal string: {text!r}")
    frac = text.partition(".")[2]
    if len(frac) > DECIMAL_PLACES:
        raise EncodingError(f"more than {DECIMAL_PLACES} fractional digits: {text!r}")
    return float(text)


def length_prefixed(*parts: str) -> bytes:
    """uint32 little-endian UTF-8 byte length + bytes, per part."""
    out = bytearray()
    for p in parts:
        b = p.encode("utf-8")
        out += struct.pack("<I", len(b)) + b
    return bytes(out)


def asset_key(server_id: str, library_id: str, asset_id: str, version_id: str) -> str:
    """Local managed-path key for an exact asset reference (lowercase SHA-256 hex)."""
    return hashlib.sha256(length_prefixed(server_id, library_id, asset_id, version_id)).hexdigest()
