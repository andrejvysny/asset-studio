"""The committed OpenAPI schema (source of the generated web types) must match the live app."""
from __future__ import annotations

import json
from pathlib import Path

from assetstudio_server.cli import build_openapi

COMMITTED = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "generated" / "openapi.json"


def test_committed_schema_is_fresh() -> None:
    live = json.loads(json.dumps(build_openapi()))
    assert json.loads(COMMITTED.read_text()) == live, "web/src/lib/generated/openapi.json is stale: run `make web-types`"


def test_operator_and_runner_path_families_present() -> None:
    paths = build_openapi()["paths"]
    for p in ("/api/v1/runners", "/api/v1/runners/{runner_id}", "/api/v1/runner-groups",
              "/api/v1/runner-groups/{group_id}/registration-tokens", "/api/v1/runners/{runner_id}:revoke",
              "/api/v1/runners/{runner_id}/push-url", "/api/v1/attempts/{attempt_id}:declare-lost"):
        assert p in paths, p
    assert any(p.startswith("/api/runner/v1/") for p in paths)
