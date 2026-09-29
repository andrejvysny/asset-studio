"""R07: publications describe what ran at execution, and a derived licence is never better than its source."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from assetstudio_server.coordinator.stages import build
from assetstudio_server.models import load_lock
from assetstudio_server.provenance import derived_licence

from tests.conftest import ROOT, Api
from tests.contract.test_api_batches import approve, confirm_all, create, detail, setup_project

CONFIG = ROOT / "config"


def _env() -> Any:
    return SimpleNamespace(studio=SimpleNamespace(settings=SimpleNamespace(config_dir=CONFIG)))


def test_publication_keeps_generation_time_models_after_lock_changes(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = setup_project(api)
    bid = create(api, pid, ["Tavern"], "rc-batch-1")["batch"]["id"]
    api.wait_ops()
    confirm_all(api, pid, bid, "rc-confirm-1")
    api.wait_ops()
    item = detail(api, pid, bid)["items"][0]
    gen = item["candidate_set"]["generation"]
    assert gen["model_receipts"][0]["key"] == "qwen_image_2512" and gen["workflow"]["id"] == "fake.t2i"
    assert gen["licence"]["status"] == "not_cleared"  # simulated engine
    recorded = gen["model_receipts"][0]["revision"]
    approve(api, pid, bid, item, 0, "rc-approve-1")
    item = detail(api, pid, bid)["items"][0]
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:build-approved", {"idempotency_key": "rc-build-1", "items": [
        {"item_id": item["id"], "approval_id": item["approval"], "expected_item_revision": item["revision"]}]})
    api.wait_ops()
    item = detail(api, pid, bid)["items"][0]
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:accept-builds", {"idempotency_key": "rc-accept-1", "items": [
        {"item_id": item["id"], "build_run_id": item["current_build"], "expected_item_revision": item["revision"]}]})

    changed = load_lock(CONFIG)
    changed["models"]["qwen_image_2512"]["revision"] = "f" * 40
    monkeypatch.setattr(build, "load_lock", lambda _d: changed)  # the lock moved on after generation
    prev = api.get(f"/api/v1/projects/{pid}/batches/{bid}/publish-preview")["items"]
    api.post(f"/api/v1/projects/{pid}/batches/{bid}:publish", {"idempotency_key": "rc-pub-1", "items": [
        {"item_id": p["item_id"], "build_run_id": p["build_run_id"], "expected_item_revision": p[
            "expected_item_revision"]} for p in prev]})
    api.wait_ops()
    lib = api.get(f"/api/v1/projects/{pid}/assets")
    v = api.get(f"/api/v1/projects/{pid}/assets/{lib['items'][0]['asset_id']}")["shown_version"]
    assert v["engine"]["models_provenance"] == "execution_receipt"
    assert v["models"][0]["revision"] == recorded != "f" * 40
    assert v["licence"]["status"] == "not_cleared"


def test_legacy_candidate_set_is_marked_reconstructed() -> None:
    models, prov = build._models_of(_env(), {"models": ["qwen_image_2512"]}, ["qwen_image_2512"])
    assert prov == "reconstructed" and models[0]["reconstructed_from_current_lock"] is True
    assert models[0]["revision"] == load_lock(CONFIG)["models"]["qwen_image_2512"]["revision"]


def test_receipts_used_verbatim_and_missing_key_flagged() -> None:
    gen = {"models": ["a", "b"], "model_receipts": [
        {"key": "a", "repo": "r/a", "revision": "abc", "files": {"x": "1"}}, {"key": "b", "missing_from_lock": True}]}
    models, prov = build._models_of(_env(), gen, gen["models"])
    assert prov == "execution_receipt"
    assert models == [{"key": "a", "repo": "r/a", "revision": "abc", "files": {"x": "1"}},
                      {"key": "b", "missing_from_lock": True}]


def test_generation_licence_is_kept_and_build_components_added() -> None:
    gen_lic = {"status": "cleared", "components": [{"id": "qwen_image_2512", "status": "cleared"}], "note": "n."}
    run = SimpleNamespace(inputs={"components": ["qwen_image_2512", "nvdiffrast_missing"]})
    out = build._licence_of(_env(), {"licence": gen_lic}, [], run)  # type: ignore[arg-type]
    ids = [c["id"] for c in out["components"]]
    assert ids == ["qwen_image_2512", "nvdiffrast_missing"] and out["status"] == "unknown"
    assert "resolved at publication" in out["note"]


@pytest.mark.parametrize("src", ["unknown", "review", "not_cleared"])
@pytest.mark.parametrize("processing", ["cleared", "unknown"])
def test_derived_licence_never_better_than_source(src: str, processing: str) -> None:
    sev = {"cleared": 0, "unknown": 1, "review": 2, "not_cleared": 3}
    out = derived_licence({"status": processing, "components": []}, {"status": src}, "x")
    assert sev[out["status"]] >= max(sev[src], sev[processing])
    assert out["source_status"] == src and out["processing_status"] == processing


def test_derived_licence_missing_source_status_is_unknown() -> None:
    assert derived_licence({"status": "cleared", "components": []}, {}, "x")["status"] == "unknown"


def test_direct_variant_of_unresolved_source_is_not_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = SimpleNamespace(id="p", source=SimpleNamespace(asset_id="a", version_id="v", licence={"status": "review"}))
    dec = SimpleNamespace(id="d", bound={"row_id": "row", "transform": {}})
    monkeypatch.setattr(build, "load_decision", lambda *_a: dec)
    item, run = SimpleNamespace(id="i", snapshot_sha="s"), SimpleNamespace(id="r", validation={}, inputs={"approval_id": "d"})
    d = build._direct_details(SimpleNamespace(ctx=SimpleNamespace(store=None)), "j", item, run, plan)  # type: ignore[arg-type]
    assert d["licence"]["status"] == "review" and d["licence"]["derivation"] == "deterministic CPU transform"
