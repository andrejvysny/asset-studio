"""The committed integration OpenAPI document matches the routes the code serves."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_openapi_document_is_current() -> None:
    spec = importlib.util.spec_from_file_location("export_openapi", ROOT / "scripts/export_integration_openapi.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.OUT.read_bytes() == module.build(), "run: uv run python scripts/export_integration_openapi.py"
