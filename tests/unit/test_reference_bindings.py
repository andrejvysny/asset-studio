"""resolve_references: candidate order, per-usage limit, exclusion records, legacy snapshots, determinism."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from assetstudio_server.services.reference_bindings import ROUTING_VERSION, resolve_references


class _Store:
    def artifact(self, artifact_id: str) -> Any:
        return SimpleNamespace(sha256=f"sha-{artifact_id}")


def _item(n: int) -> Any:
    return SimpleNamespace(references=[{"id": f"jrf_{i}", "artifact_id": f"art_i{i}", "sha256": f"s{i}",
                                        "crop": None, "note": f"n{i}"} for i in range(n)])


def _snap(mode: str = "prompt_guidance", routed: bool = True, images: int = 2) -> dict[str, Any]:
    snap: dict[str, Any] = {"values": {"reference_set": "mood"}, "reference_set": {
        "label": "Mood", "mode": mode, "images": [{"artifact_id": f"art_s{i}", "label": f"L{i}" if i else "",
                                                    "role": "style"} for i in range(images)]}}
    if routed:
        snap["reference_routing"] = ROUTING_VERSION
    return snap


def test_item_refs_first_then_set_in_order() -> None:
    sel = resolve_references(_Store(), _item(1), _snap(), "prompt_guidance", 4)  # type: ignore[arg-type]
    assert sel.ids() == ["jrf_0", "set:mood:0", "set:mood:1"] and sel.excluded_list() == []
    assert [b.origin for b in sel.selected] == ["item", "project_set", "project_set"]
    assert sel.selected[1].sha256 == "sha-art_s0" and sel.selected[1].note == "style"  # empty label -> role
    assert sel.selected[2].note == "L1"


def test_limit_excludes_with_reason() -> None:
    sel = resolve_references(_Store(), _item(3), _snap(), "prompt_guidance", 3)  # type: ignore[arg-type]
    assert sel.ids() == ["jrf_0", "jrf_1", "jrf_2"]
    assert sel.excluded_list() == [{"id": f"set:mood:{i}", "reason": "over the 3-image limit for prompt_guidance"}
                                   for i in range(2)]


def test_other_mode_set_not_used_for_this_usage() -> None:
    sel = resolve_references(_Store(), _item(1), _snap("qa_reference"), "prompt_guidance", 4)  # type: ignore[arg-type]
    assert sel.ids() == ["jrf_0"] and sel.excluded_list() == []
    sel = resolve_references(_Store(), _item(0), _snap("qa_reference"), "qa_reference", 4)  # type: ignore[arg-type]
    assert sel.ids() == ["set:mood:0", "set:mood:1"]


def test_legacy_snapshot_excludes_set_images() -> None:
    sel = resolve_references(_Store(), _item(1), _snap(routed=False), "prompt_guidance", 4)  # type: ignore[arg-type]
    assert sel.ids() == ["jrf_0"]
    assert [e["id"] for e in sel.excluded_list()] == ["set:mood:0", "set:mood:1"]
    assert all("not routed" in e["reason"] for e in sel.excluded_list())


def test_no_set_and_determinism() -> None:
    snap = {"values": {"reference_set": None}, "reference_set": None, "reference_routing": 1}
    assert resolve_references(_Store(), _item(2), snap, "qa_reference", 4).ids() == ["jrf_0", "jrf_1"]  # type: ignore[arg-type]
    a = resolve_references(_Store(), _item(2), _snap(), "prompt_guidance", 2)  # type: ignore[arg-type]
    b = resolve_references(_Store(), _item(2), _snap(), "prompt_guidance", 2)  # type: ignore[arg-type]
    assert a == b


class _MissingStore(_Store):
    def artifact(self, artifact_id: str) -> Any:
        from assetstudio_storage.repo import NotFound

        if artifact_id == "art_s0":
            raise NotFound(artifact_id)
        return super().artifact(artifact_id)


def test_missing_set_artifact_is_excluded_not_raised() -> None:
    sel = resolve_references(_MissingStore(), _item(0), _snap(), "prompt_guidance",  # type: ignore[arg-type]
                             4)
    assert sel.ids() == ["set:mood:1"]
    assert sel.excluded_list()[0]["id"] == "set:mood:0" and "missing" in sel.excluded_list()[0]["reason"]
