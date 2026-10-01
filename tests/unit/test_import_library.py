"""scripts/import_library.py: discovery, descriptor drafts, slot roles and resume logic (fake transport, no network)."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import import_library as il  # noqa: E402

GLB_DIR = ROOT / "contracts/godot-integration/v1/fixtures/glb"
PROP = GLB_DIR / "primitive_prop.portable.glb"
PROP2 = GLB_DIR / "primitive_prop_v2.portable.glb"
LIB, SERVER = "prj_0000000000000001", "11111111-2222-3333-4444-555555555555"


def _tree(tmp_path: Path) -> Path:
    src = tmp_path / "lib"
    files = {
        "assets/nature/rocks/boulder_A.glb": PROP, "assets/nature/rocks/previews/p.glb": PROP,
        "assets/nature/rocks/source/s.glb": PROP, "assets/nature/rocks/boulder_A.glb.import": PROP,
        "assets/nature/trees/pine_A.glb": PROP2, "spruce_trees/models/spruce_x.glb": PROP,
        "spruce_trees/models/spruce_x.glb.import": PROP, "props/campfire.glb": PROP, "grass/glb/tuft.glb": PROP,
        "characters/character.glb": PROP, "materials/m.glb": PROP, "tools/t.glb": PROP,
    }
    for rel, origin in files.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(origin, src / rel)
    (src / "assets/nature/rocks/boulder_A.json").write_text(json.dumps({
        "asset_id": "boulder_A", "family": "boulder", "group": "rocks", "habitats": ["Rocky Slope", "exposed"],
        "bbox_min": [-1.5, -0.1, -2.25], "bbox_max": [1.25, 2.0, 1.0], "scale_range": [0.85, 1.2]}))
    (src / "spruce_trees/models/variants.json").write_text(json.dumps({"variants": [
        {"name": "spruce_x", "file": "spruce_x.glb", "kind": "dead", "group": "sizes", "crown_radius_m": 1.1238}]}))
    return src


def test_discovery_lists_only_static_models(tmp_path: Path) -> None:
    entries = il.discover(_tree(tmp_path))
    assert [(e.relpath, e.kind) for e in entries] == [
        ("assets/nature/rocks/boulder_A.glb", "nature"), ("assets/nature/trees/pine_A.glb", "nature"),
        ("characters/character.glb", "character"), ("grass/glb/tuft.glb", "grass"),
        ("props/campfire.glb", "prop"), ("spruce_trees/models/spruce_x.glb", "spruce")]
    assert entries[0].metadata["family"] == "boulder" and entries[-1].metadata["kind"] == "dead"
    assert il.idempotency_key(entries[0]) == "fgl-assets-nature-rocks-boulder-a-v1"


def test_tags_and_names(tmp_path: Path) -> None:
    rock, _, _, _, _, spruce = il.discover(_tree(tmp_path))
    assert il.tags_for(rock) == ["boulder", "exposed", "nature", "rocks", "rocky_slope"]
    assert il.tags_for(spruce) == ["dead", "sizes", "spruce"]
    assert (il.asset_name(rock), il.asset_name(spruce)) == ("boulder_A", "spruce_x")


def test_draft_for_nature_uses_json_bbox_and_scale(tmp_path: Path) -> None:
    src = _tree(tmp_path)
    plan = il.make_plan(src, il.discover(src)[0], il.load_limits())
    draft = json.loads(plan.draft)
    assert draft["footprint_radius_m"] == "2.25" and draft["scale_range"] == ["0.85", "1.2"]
    assert draft["placement_anchor"] == ["0", "0", "0"] and draft["collision"] is None
    assert draft["default_grounding"] == "FOLLOW_TERRAIN" and draft["height_offset_range_m"] == ["-0.5", "0.5"]
    assert draft["material_slots"] == [{"slot_id": "default", "role": "solid",
                                        "surfaces": {"portable_glb_v1": [{"mesh": 0, "primitive": 0}]}}]
    assert plan.commit["source_uri"] == "fantasy-game-library/assets/nature/rocks/boulder_A.glb"


def test_draft_measures_bounds_and_spruce_radius(tmp_path: Path) -> None:
    src = _tree(tmp_path)
    plans = {e.kind: il.make_plan(src, e, il.load_limits()) for e in il.discover(src)}
    assert json.loads(plans["prop"].draft)["footprint_radius_m"] == "1"  # measured from the GLB: max(|x|,|z|) = 1
    assert json.loads(plans["prop"].draft)["scale_range"] == ["0.5", "2"]
    spruce = json.loads(plans["spruce"].draft)
    assert spruce["footprint_radius_m"] == "1.1238" and spruce["scale_range"] == ["0.8", "1.25"]
    two = il.make_plan(src, il.discover(src)[1], il.load_limits())  # two meshes, no materials: one shared slot
    assert [len(s["surfaces"]["portable_glb_v1"]) for s in json.loads(two.draft)["material_slots"]] == [2]


def test_slot_roles_and_ids() -> None:
    doc = {"materials": [{"name": "M_solid"}, {"name": "M_Foliage"}, {"name": "Pine Needles"}, {"name": "M_solid"},
                         {"name": "unused"}],
           "meshes": [{"primitives": [{"material": 0}, {"material": 1}]},
                      {"primitives": [{"material": 2}, {"material": 3}, {}]}]}
    slots = il.material_slots(doc)
    assert [(s["slot_id"], s["role"]) for s in slots] == [
        ("m_solid", "solid"), ("m_foliage", "foliage"), ("pine_needles", "foliage"), ("m_solid_2", "solid"),
        ("default", "solid")]
    assert slots[0]["surfaces"]["portable_glb_v1"] == [{"mesh": 0, "primitive": 0}]


def test_non_static_glb_fails(tmp_path: Path) -> None:
    src = tmp_path / "bad"
    (src / "props").mkdir(parents=True)
    (src / "props/broken.glb").write_bytes(b"not a glb")
    with pytest.raises(il.ImportFailure):
        il.make_plan(src, il.discover(src)[0], il.load_limits())


class FakeApi:
    """In-memory server: idempotency key -> committed publication; commit binds the preview id like the real one."""

    def __init__(self) -> None:
        self.committed: dict[str, dict[str, str]] = {}
        self.bound: dict[str, str] = {}
        self.calls: list[str] = []
        self.expired: set[str] = set()
        self.previews = 0

    def server_id(self) -> str:
        return SERVER

    def operation(self, key: str) -> dict[str, Any]:
        self.calls.append("operation")
        if key in self.committed:
            return {"state": "committed", **self.committed[key], "display_version": 1}
        return {"state": "unknown"}

    def preview(self, glb: bytes, draft: bytes) -> dict[str, Any]:
        import hashlib
        self.calls.append("preview")
        self.previews += 1
        return {"preview_id": f"ipv_{self.previews}", "portable_sha256": hashlib.sha256(glb).hexdigest(),
                "descriptor_draft_sha256": hashlib.sha256(draft).hexdigest()}

    def commit(self, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("commit")
        key = body["idempotency_key"]
        if body["preview_id"] in self.expired:
            raise il.ApiError(422, "preview_expired", "expired")
        if self.bound.setdefault(key, body["preview_id"]) != body["preview_id"]:
            raise il.ApiError(409, "idempotency_conflict", "different request")
        ref = {"asset_id": f"ast_{len(self.committed)}", "version_id": "ver_1"}
        self.committed[key] = ref
        return {"asset_ref": {"server_id": SERVER, "library_id": LIB, **ref}, "display_version": 1,
                "descriptor_sha256": "d" * 64}

    def resolve(self, refs: list[dict[str, str]]) -> list[dict[str, Any]]:
        return [{"state": "ready" if r["asset_id"] != "ast_bad" else "not_found", "error": None,
                 "descriptor_sha256": "d" * 64,
                 "deliveries": [{"delivery_id": "dlv_1", "representation": "portable_glb_v1"}]} for r in refs]


def _plan(tmp_path: Path, index: int = 0) -> il.Plan:
    src = _tree(tmp_path)
    return il.make_plan(src, il.discover(src)[index], il.load_limits())


def test_import_then_rerun_is_noop(tmp_path: Path) -> None:
    src, api = _tree(tmp_path), FakeApi()
    state = il.State(tmp_path / "s.json", LIB)
    out = (tmp_path / "out.txt").open("w")
    tally = il.run_import(src, api, state, None, out)
    assert sum(t.get("committed", 0) for t in tally.values()) == 6 and len(api.committed) == 6
    calls = len(api.calls)
    tally = il.run_import(src, api, il.State(tmp_path / "s.json", LIB), None, out)  # reloaded from disk
    assert all(set(t) == {"skipped"} for t in tally.values()) and len(api.calls) == calls


def test_lost_state_recovers_from_server_operation(tmp_path: Path) -> None:
    src, api = _tree(tmp_path), FakeApi()
    out = (tmp_path / "out.txt").open("w")
    il.run_import(src, api, il.State(tmp_path / "a.json", LIB), "props/*", out)
    previews = api.previews
    state = il.State(tmp_path / "fresh.json", LIB)
    tally = il.run_import(src, api, state, "props/*", out)
    assert tally == {"prop": {"committed": 1}} and api.previews == previews  # no new preview, no duplicate version
    assert state.get("props/campfire.glb")["asset_ref"]["asset_id"] == "ast_0"


def test_previewed_state_commits_with_saved_preview_then_falls_back_when_expired(tmp_path: Path) -> None:
    plan, api = _plan(tmp_path, 4), FakeApi()
    state = il.State(tmp_path / "s.json", LIB)
    il._preview(api, state, plan)
    assert state.get(plan.entry.relpath)["state"] == "previewed"
    assert il.publish_one(api, state, plan) == "committed" and api.previews == 1  # reused the saved preview
    plan2, state2 = _plan(tmp_path / "x", 3), il.State(tmp_path / "s2.json", LIB)
    il._preview(api, state2, plan2)
    api.expired.add(state2.get(plan2.entry.relpath)["preview_id"])
    assert il.publish_one(api, state2, plan2) == "committed" and api.previews == 3  # re-previewed after expiry


def test_changed_source_is_conflict_not_bumped(tmp_path: Path) -> None:
    plan, api = _plan(tmp_path), FakeApi()
    state = il.State(tmp_path / "s.json", LIB)
    assert il.publish_one(api, state, plan) == "committed"
    plan.sha256 = "0" * 64
    assert il.publish_one(api, state, plan) == "conflict"
    assert state.get(plan.entry.relpath)["state"] == "conflict" and len(api.committed) == 1


def test_server_conflict_and_failures_are_recorded(tmp_path: Path) -> None:
    plan, api = _plan(tmp_path), FakeApi()
    api.bound[plan.commit["idempotency_key"]] = "ipv_other"
    state = il.State(tmp_path / "s.json", LIB)
    assert il.publish_one(api, state, plan) == "conflict"
    assert "idempotency_conflict" in state.get(plan.entry.relpath)["error"]


def test_verify_records_ready_and_flags_not_ready(tmp_path: Path) -> None:
    state = il.State(tmp_path / "s.json", LIB)
    ref = {"server_id": SERVER, "library_id": LIB, "version_id": "ver_1"}
    state.update("a.glb", state="committed", asset_ref={**ref, "asset_id": "ast_ok"})
    state.update("b.glb", state="committed", asset_ref={**ref, "asset_id": "ast_bad"})
    state.update("c.glb", state="failed")
    out = (tmp_path / "o.txt").open("w")
    assert il.run_verify(FakeApi(), state, None, out) == 1
    assert state.get("a.glb")["state"] == "verified" and state.get("a.glb")["portable_delivery_id"] == "dlv_1"
    assert state.get("b.glb")["state"] == "committed" and "not_found" in state.get("b.glb")["error"]


def test_state_file_is_private_and_library_bound(tmp_path: Path) -> None:
    path = tmp_path / "d" / "s.json"
    il.State(path, LIB).update("a.glb", state="planned")
    assert path.stat().st_mode & 0o777 == 0o600 and not path.with_name("s.json.tmp").exists()
    with pytest.raises(il.ImportFailure):
        il.State(path, "prj_other")


def test_dry_run_reports_without_network(tmp_path: Path) -> None:
    src = _tree(tmp_path)
    out = (tmp_path / "o.txt")
    with out.open("w") as fp:
        assert il.run_dry(src, il.State(tmp_path / "s.json", LIB), None, fp) == 0
    text = out.read_text()
    assert "total 6: would-publish 6" in text and "assets/nature/rocks/boulder_A.glb" in text
