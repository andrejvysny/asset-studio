"""Godot (GDScript) reproduces the Python golden vectors for asset keys, raw-byte hashing and decimals."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT / "integrations" / "godot"
CONTRACTS = ROOT / "contracts" / "godot-integration" / "v1"


def test_godot_reproduces_canonical_vectors() -> None:
    godot = os.environ.get("GODOT_BIN") or shutil.which("godot")
    if not godot:
        pytest.skip("godot binary not found (set GODOT_BIN)")
    proc = subprocess.run(
        [godot, "--headless", "--path", str(PROJECT), "--script", "res://tests/run_tests.gd"],
        env={**os.environ, "ASSETSTUDIO_CONTRACTS_DIR": str(CONTRACTS)},
        capture_output=True, text=True, timeout=120, check=False,
    )
    tail = f"stdout:\n{proc.stdout[-3000:]}\nstderr:\n{proc.stderr[-2000:]}"
    assert proc.returncode == 0, tail
    assert "FAILED 0" in proc.stdout, tail
