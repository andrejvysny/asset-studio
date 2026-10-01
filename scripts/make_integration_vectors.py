"""Regenerate the Godot integration v1 golden vectors (canonical-v1.json, canonical-json-v1.json).

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
OUT_JSON = OUT.with_name("canonical-json-v1.json")
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


# Tagged encoding keeps control characters, NUL and non-ASCII intact through any JSON reader:
# {"n":null} {"b":bool} {"i":"<decimal int>"} {"s":[code points]} {"a":[tagged]} {"o":[{"k":[code points],"v":tagged}]}
# (object pairs are listed in deliberately unsorted order); {"f":"<repr>"} is a float and must be rejected.
def _s(text: str) -> dict:
    return {"s": [ord(c) for c in text]}


def _tag(v: object) -> dict:
    if v is None:
        return {"n": None}
    if isinstance(v, bool):
        return {"b": v}
    if isinstance(v, int):
        return {"i": str(v)}
    if isinstance(v, float):
        return {"f": repr(v)}
    if isinstance(v, str):
        return _s(v)
    if isinstance(v, list):
        return {"a": [_tag(x) for x in v]}
    return {"o": [{"k": [ord(c) for c in k], "v": _tag(x)} for k, x in v.items()]}


def _json_cases() -> list[tuple[str, object]]:
    cases: list[tuple[str, object]] = [(f"control U+{i:04X}", f"a{chr(i)}b") for i in range(0x20)]
    cases += [
        ("quote backslash slash", ["\"", "\\", "/"]), ("DEL U+007F", "\x7f"), ("U+2028 U+2029", "\u2028\u2029"),
        ("emoji and bmp", "\U0001f332 \u010d\u00fd\u00e1"), ("max code point", "\U0010ffff"),
        ("nested", {"b": [3, {"z": True, "a": None}, [[], {}]], "a": "x", "c": {"d": {"e": []}}}),
        ("ints", [0, 1, -1, 255, -255, 2**53, -(2**53), 9007199254740991, 1000000]),
        ("bools and null", [True, False, None]), ("empty containers", {"a": [], "b": {}, "c": ""}),
        ("empty object", {}), ("empty array", []), ("empty string", ""),
        ("key order ascii", {"b": 1, "a": 2, "B": 3, "_": 4, "a1": 5, "A": 6, "": 7}),
        ("key order code point not utf16",
         {"\U0001f332": 1, "\uffee": 2, "\ue000": 3, "z": 4, "\u010d": 5, "\u00fd": 6}),
        ("key order prefix", {"ab": 1, "a": 2, "abc": 3}),
        ("escaped keys", {"a\nb": 1, "a\"b": 2, "a\\b": 3, "a\x01b": 4}),
        ("digit-like strings", {"10": "2", "9": "1", "1": "3"}),
    ]
    return cases


FLOAT_REJECTS = [("non-integral 0.5", 0.5), ("tiny 1e-7", 1e-7), ("negative 1.5", -1.5), ("nan", float("nan")),
                 ("inf", float("inf")), ("-inf", float("-inf")), ("nested float", [1, {"x": 2.25}])]


def build_json() -> bytes:
    cases = []
    for name, obj in _json_cases():
        raw = canonical_bytes(obj)
        cases.append({"name": name, "input": _tag(obj), "expected_utf8_hex": raw.hex()})
    rejects = [{"name": n, "input": _tag(v)} for n, v in FLOAT_REJECTS]
    doc = {"schema": "godot-integration/v1/canonical-json-vectors", "cases": cases, "reject": rejects}
    return pretty_json(doc)


def main() -> int:
    data, json_data = build(), build_json()
    if "--check" in sys.argv:
        ok = OUT.exists() and OUT.read_bytes() == data and OUT_JSON.exists() and OUT_JSON.read_bytes() == json_data
        return 0 if ok else 1
    OUT.write_bytes(data)
    OUT_JSON.write_bytes(json_data)
    print(OUT, OUT_JSON)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
