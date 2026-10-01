"""Export the integration listener's OpenAPI document to the contract bundle (no network, no GPU).

Usage: uv run python scripts/export_integration_openapi.py [--check]
--check exits non-zero when the committed document differs from the running code's routes.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from assetstudio_core.canonical import pretty_json
from assetstudio_server.integration_api.app import build_integration_app
from assetstudio_server.settings import Settings
from assetstudio_server.studio import build_studio

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "contracts/godot-integration/v1/integration-api.openapi.json"


def build() -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        s = Settings()
        s.instance_dir, s.project_roots = Path(tmp) / "instance", [Path(tmp) / "projects"]
        s.engine, s.start_coordinator, s.contracts_dir = "none", False, OUT.parent
        studio = build_studio(s)
        try:
            doc = build_integration_app(studio, s).fastapi.openapi()
        finally:
            studio.close()
    doc["info"]["version"] = "1"  # API contract version, not the package version
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
