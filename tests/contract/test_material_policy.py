"""Build profile material policy on the delivered GLB; material-only rebuilds reuse the bake (no worker call).
SIMULATED worker (textured sphere): contract evidence, verified from the output GLB bytes."""
from __future__ import annotations

from typing import Any

from assetstudio_processing.materials import read_glb

from tests.conftest import Api
from tests.contract.test_api_model3d import _budget_project, _built
from tests.contract.test_api_variant_qa import _build, _w3d

P = "/api/v1/projects"
FOLIAGE = {"label": "Foliage", "geometry": {"small_components": "preserve", "expect_single_component": False},
           "material": {"alpha_mode": "mask", "alpha_cutoff": 0.4, "double_sided": True, "roughness_min": 0.8,
                        "metallic": 0.0}}


def _profile_project(api: Api, profile: dict[str, Any] = FOLIAGE) -> str:
    pid = _budget_project(api)
    view = api.get(f"{P}/{pid}/config")
    cfg = view["config"]
    cfg["build_profiles"] = {"foliage": profile}
    crates = next(c for c in cfg["categories"] if c["id"] == "crates")
    crates["defaults"]["build_profile"] = {"mode": "value", "value": "foliage"}
    r = api.raw("PATCH", f"{P}/{pid}/config", json={"expected_revision": view["revision"], "config": cfg})
    assert r.status_code == 200, r.text
    return pid


def _material(api: Api, pid: str, build: dict) -> dict:
    data = api.raw("GET", f"{P}/{pid}/artifacts/{build['artifacts']['model']}/content").content
    return read_glb(data)[0]["materials"][0]


def test_profile_material_reaches_delivered_glb(api: Api) -> None:
    pid = _profile_project(api)
    _, it = _built(api, pid, "crates", "mat-1")
    b = it["build"]
    assert b["result"] == "valid", b["validation"]
    m = _material(api, pid, b)
    assert m["alphaMode"] == "MASK" and m["alphaCutoff"] == 0.4 and m["doubleSided"] is True
    assert m["pbrMetallicRoughness"]["metallicFactor"] == 0.0 and m["pbrMetallicRoughness"]["roughnessFactor"] == 1.0
    checks = {c["id"]: c for c in b["validation"]["checks"]}
    assert checks["material_policy"]["ok"] and checks["alpha_mode"]["ok"]
    assert "single_component" not in checks  # multi-part foliage is expected by the profile
    assert "model_unmaterialized" not in b["artifacts"] or b["artifacts"]["model_unmaterialized"] != \
        b["artifacts"]["model"]
    assert set(b["checkpoints"]) >= {"bake", "material"}


def test_no_profile_leaves_material_untouched(api: Api) -> None:
    pid = _budget_project(api)
    _, it = _built(api, pid, "crates", "mat-none")
    assert "material" not in it["build"]["checkpoints"]
    assert _material(api, pid, it["build"]).get("alphaMode", "OPAQUE") == "OPAQUE"


def test_material_only_rebuild_reuses_bake(api: Api) -> None:
    pid = _profile_project(api)
    bid, first = _built(api, pid, "crates", "mat-rb")
    calls = list(_w3d(api).calls)
    second = _build(api, pid, bid, "mat-rebuild-1", mode="rebuild",
                    overrides={"alpha_mode": "blend", "double_sided": False})
    b = second["build"]
    assert b["result"] == "valid", b["validation"]
    assert _w3d(api).calls == calls  # no generate, no export: only the CPU material stage ran
    assert b["checkpoints"]["bake"]["outputs"] == first["build"]["checkpoints"]["bake"]["outputs"]
    m = _material(api, pid, b)
    assert m["alphaMode"] == "BLEND" and "alphaCutoff" not in m and m["doubleSided"] is False


def test_geometry_rebuild_exports_again(api: Api) -> None:
    pid = _profile_project(api)
    bid, _ = _built(api, pid, "crates", "geo-rb")
    exports = sum(1 for c in _w3d(api).calls if c.startswith("export"))
    second = _build(api, pid, bid, "geo-rebuild-1", mode="rebuild", overrides={"fill_holes": "disabled"})
    assert second["build"]["result"] == "valid", second["build"]["validation"]
    assert sum(1 for c in _w3d(api).calls if c.startswith("export")) == exports + 1
    assert _w3d(api).calls.count("generate") == 1
    assert second["build"]["checkpoints"]["bake"]["settings"]["fill_holes"] == "disabled"


def test_invalid_profile_overrides_rejected(api: Api) -> None:
    pid = _profile_project(api)
    bid, _ = _built(api, pid, "crates", "mat-bad")
    it = api.get(f"/api/v2/projects/{pid}/jobs/{bid}")["items"][0]
    for bad in ({"roughness_min": 2.0}, {"alpha_mode": "blend", "alpha_cutoff": 0.5}, {"alpha_mode": "glass"}):
        r = api.raw("POST", f"{P}/{pid}/batches/{bid}:build-approved", json={
            "idempotency_key": f"bad-{sorted(bad)[0]}-{len(bad)}", "items": [{
                "item_id": it["id"], "approval_id": it["approval"], "expected_item_revision": it["revision"],
                "mode": "rebuild", "overrides": bad}]})
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_parameters", (bad, r.text)
