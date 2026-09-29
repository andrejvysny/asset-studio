"""Deterministic seeds from a persisted seed family + stable ids (never list position, never Python hash())."""
from __future__ import annotations

import hashlib
import secrets

MAX_SEED = 2**53 - 1  # exact in JSON/JS numbers; ComfyUI accepts up to 2**64-1


def new_seed_family() -> int:
    return secrets.randbelow(MAX_SEED)


def derive_seed(seed_family: int, *parts: str) -> int:
    h = hashlib.blake2b(digest_size=8, person=b"assetstudio-v1")
    h.update(str(seed_family).encode())
    for p in parts:
        h.update(b"\x00" + p.encode())
    return int.from_bytes(h.digest(), "big") & MAX_SEED
