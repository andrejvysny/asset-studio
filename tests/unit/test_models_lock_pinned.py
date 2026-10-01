"""R10: runners admit a model only after full-hash verification, so every catalog file must carry a pinned sha256."""
from __future__ import annotations

import re
from pathlib import Path

from assetstudio_server.models import load_lock

ROOT = Path(__file__).resolve().parents[2]
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def test_every_model_file_has_a_pinned_sha256_and_size() -> None:
    lock = load_lock(ROOT / "config")
    unpinned = [f"{key}/{name}" for key, entry in lock["models"].items()
                for name, spec in (entry.get("files") or {}).items()
                if not SHA256.match(str((spec or {}).get("sha256") or "")) or not isinstance(spec.get("size"), int)]
    assert not unpinned, f"pin sha256 + size for: {unpinned}"
    assert all(entry.get("files") for entry in lock["models"].values()), "a model entry lists no files"


def test_loras_reference_pinned_model_files() -> None:
    lock = load_lock(ROOT / "config")
    for key, lora in (lock.get("loras") or {}).items():
        files = lock["models"][lora["model"]]["files"]
        assert lora["file"] in files, f"lora {key} points at an unpinned file"
