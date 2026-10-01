"""Stable, opaque, prefixed identifiers. Never derived from mutable names or category paths."""
from __future__ import annotations

import re
import secrets

PREFIXES = {
    "prj", "cat", "shot", "ast", "ver", "bat", "itm", "prm", "cs", "cnd", "qa", "dec", "run", "op", "art", "imp", "exp",
    "ref", "style", "qrs",
    # Jobs/Batches milestone. `bat` stays valid: legacy production batches ARE Jobs (never new grouping Batches).
    "job", "bch", "brn", "wav", "stk", "pas", "att", "sty", "san", "xpl", "xrn", "cmd", "sel", "upl", "aud",
    # Variants/families milestone.
    "fam", "vdr", "vpl", "srs", "vsa", "div", "row", "jrf",
    # Media library.
    "med",
    # Compute runners.
    "rnr", "rgp", "rse", "atp", "xfr",
    # Godot integration deliveries.
    "dlv",
    # Godot integration publication previews (staged, expiring).
    "ipv",
    # Godot integration client credentials (immutable identity; names are reusable display labels).
    "icr",
}
JOB_PREFIXES = ("job", "bat")
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32, lowercase
_ID_RE = re.compile(r"^([a-z]{2,5})_([0-9a-hjkmnp-tv-z]{16})$")
# Human-chosen ids inside studio.yaml (categories, rule sets, presets): slug-like, stable once created.
CONFIG_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class InvalidId(ValueError):
    pass


def new_id(prefix: str) -> str:
    if prefix not in PREFIXES:
        raise InvalidId(f"unknown id prefix {prefix!r}")
    n = secrets.randbits(80)
    body = "".join(_ALPHABET[(n >> (5 * i)) & 31] for i in range(16))
    return f"{prefix}_{body}"


def validate_id(value: object, prefix: str | None = None) -> str:
    if not isinstance(value, str):
        raise InvalidId("id must be a string")
    m = _ID_RE.fullmatch(value)
    if not m or m.group(1) not in PREFIXES or (prefix is not None and m.group(1) != prefix):
        raise InvalidId(f"invalid {prefix or ''} id: {value!r}".replace("  ", " "))
    return value


def is_id(value: object, prefix: str | None = None) -> bool:
    try:
        validate_id(value, prefix)
        return True
    except InvalidId:
        return False


def validate_config_key(value: object) -> str:
    if not isinstance(value, str) or not CONFIG_KEY_RE.fullmatch(value):
        raise InvalidId(f"invalid key {value!r}: use lowercase letters, digits, '_', '-', '.'")
    return value


def derived_id(prefix: str, *parts: str) -> str:
    """Deterministic id from other stable ids: retrying a command after a crash reproduces the same identity."""
    import hashlib

    if prefix not in PREFIXES:
        raise InvalidId(f"unknown id prefix {prefix!r}")
    h = hashlib.blake2b("\x00".join((prefix, *parts)).encode(), digest_size=10).digest()
    n = int.from_bytes(h, "big")
    return f"{prefix}_" + "".join(_ALPHABET[(n >> (5 * i)) & 31] for i in range(16))


def is_job_id(value: object) -> bool:
    return any(is_id(value, p) for p in JOB_PREFIXES)


def validate_job_id(value: object) -> str:
    if not is_job_id(value):
        raise InvalidId(f"invalid job id: {value!r}")
    return value  # type: ignore[return-value]
