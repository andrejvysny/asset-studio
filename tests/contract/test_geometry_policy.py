"""Geometry cleanup policy: profile -> export params; no profile -> legacy params; older worker -> typed failure.
SIMULATED worker: contract evidence only."""
from __future__ import annotations

from typing import Any

import pytest
from assetstudio_core import inheritance

from tests.conftest import Api
from tests.contract.test_api_model3d import _budget_project, _built

LEGACY = {"exporter", "decimation_target", "texture_size", "remesh"}
POLICY = {"small_components": "preserve", "fill_holes": "disabled"}


def _with_profile(monkeypatch: pytest.MonkeyPatch, geometry: dict[str, Any]) -> None:
    real = inheritance.build_snapshot

    def patched(*a: Any, **k: Any) -> dict[str, Any]:
        snap = real(*a, **k)
        snap["build_profile"] = {"label": "test", "geometry": geometry, "material": {}}
        return snap

    monkeypatch.setattr("assetstudio_server.services.jobs.build_snapshot", patched, raising=False)


def test_no_profile_sends_exactly_legacy_params(api: Api) -> None:
    pid = _budget_project(api)
    _built(api, pid, "crates", "gp-none")
    assert [set(p) for p in api.studio.worker3d.export_params] == [LEGACY]  # type: ignore[union-attr]


def test_profile_policy_reaches_export(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    _with_profile(monkeypatch, {**POLICY, "expect_single_component": None})
    pid = _budget_project(api)
    _, it = _built(api, pid, "crates", "gp-set")
    assert it["build"]["result"] == "valid", it["build"]["validation"]
    sent = api.studio.worker3d.export_params[-1]  # type: ignore[union-attr]
    assert set(sent) == LEGACY | set(POLICY) and {k: sent[k] for k in POLICY} == POLICY


def test_partial_profile_sends_only_set_keys(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    _with_profile(monkeypatch, {"small_components": "preserve", "fill_holes": None})
    pid = _budget_project(api)
    _built(api, pid, "crates", "gp-part")
    assert set(api.studio.worker3d.export_params[-1]) == LEGACY | {"small_components"}  # type: ignore[union-attr]


def test_worker_without_feature_fails_before_generate(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    _with_profile(monkeypatch, POLICY)
    api.studio.worker3d.export_features = []  # type: ignore[union-attr]
    pid = _budget_project(api)
    _, it = _built(api, pid, "crates", "gp-old")
    assert [c for c in api.studio.worker3d.calls if c != "unload"] == []  # type: ignore[union-attr]
    assert it["build"]["validation"]["failure_code"] == "worker_feature_missing", it["build"]
