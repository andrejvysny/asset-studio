"""Regenerate the Godot integration v1 golden vectors (asset keys, decimals, canonical bytes).

Usage: uv run python scripts/make_integration_vectors.py [--check]
--check exits non-zero when the committed vectors differ (used by tests).
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from assetstudio_core.canonical import pretty_json
from assetstudio_core.canonical_v1 import asset_key, canonical_bytes, decimal_str, length_prefixed

OUT = Path(__file__).resolve().parents[1] / "contracts/godot-integration/v1/fixtures/vectors/canonical-v1.json"
SERVER = "6f1c2a52-3c2e-4d4b-9a57-0b6f6f0c1d2e"
LIB_A, LIB_B = "prj_0000000000000001", "prj_0000000000000002"

KEY_CASES = [
    ("basic", SERVER, LIB_A, "ast_00000000000000aa", "ver_00000000000000v1"),
    ("second version", SERVER, LIB_A, "ast_00000000000000aa", "ver_00000000000000v2"),
    ("same ids other library", SERVER, LIB_B, "ast_00000000000000aa", "ver_00000000000000v1"),
    ("other server", "00000000-0000-4000-8000-000000000000", LIB_A, "ast_00000000000000aa", "ver_00000000000000v1"),
    ("non-ascii", SERVER, "knižnica-č", "strom_🌲", "verzia-1"),
    ("boundary shift a", SERVER, "ab", "c", "d"),
    ("boundary shift b", SERVER, "a", "bc", "d"),
    ("empty parts", "", "", "", ""),
]
DECIMAL_CASES = [
    (0.0, "nearest"), (-0.0, "nearest"), (1.0, "nearest"), (-2.5, "nearest"), (0.1, "nearest"), (1e-7, "nearest"),
    (-1e-7, "nearest"), (123456.0000004, "nearest"), (0.0000005, "nearest"), (0.0000015, "nearest"),
    (0.1234567, "floor"), (0.1234561, "ceil"), (-0.1234567, "floor"), (-0.1234561, "ceil"), (100.0, "nearest"),
]
CANONICAL_CASES = [
    ("empty object", {}),
    ("sorted keys and nesting", {"b": [3, {"z": True, "a": None}], "a": "x"}),
    ("unicode raw utf-8", {"name": "Smrek čierny \U0001f332", "q": "\"quote\" \\ /"}),
    ("decimal strings", {"bounds_min": ["-0.5", "0", "-0.25"], "bounds_max": ["0.5", "2", "0.25"]}),
]


def build() -> bytes:
    keys = [{"name": n, "server_id": s, "library_id": lib, "asset_id": a, "version_id": v,
             "length_prefixed_hex": length_prefixed(s, lib, a, v).hex(), "asset_key": asset_key(s, lib, a, v)}
            for n, s, lib, a, v in KEY_CASES]
    decimals = [{"input_repr": repr(x), "rounding": r, "expected": decimal_str(x, r)} for x, r in DECIMAL_CASES]
    canon = []
    for n, obj in CANONICAL_CASES:
        raw = canonical_bytes(obj)
        canon.append({"name": n, "utf8": raw.decode(), "sha256": hashlib.sha256(raw).hexdigest()})
    doc = {"schema": "godot-integration/v1/vectors", "asset_keys": keys, "decimals": decimals, "canonical": canon}
    return pretty_json(doc)


def main() -> int:
    data = build()
    if "--check" in sys.argv:
        return 0 if OUT.exists() and OUT.read_bytes() == data else 1
    OUT.write_bytes(data)
    print(OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
