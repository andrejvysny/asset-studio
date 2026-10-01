"""Immutable descriptor/delivery storage: first freeze wins, idempotent deliveries, markers, no history edits."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from assetstudio_storage import delivery as d
from assetstudio_storage.local import LocalBackend
from assetstudio_storage.project import ProjectStore
from assetstudio_storage.repo import IntegrityError

AST, VER, DLV = "ast_0000000000000001", "ver_0000000000000001", "dlv_0000000000000001"


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(LocalBackend(tmp_path), "prj_0000000000000000")


def _fields() -> dict:
    return {"delivery_id": DLV, "asset_id": AST, "version_id": VER, "representation": "portable_glb_v1",
            "profile_id": "p", "profile_version": "1", "total_bytes": 3, "budget": {"within_ipad_budget": True}}


def test_freeze_first_wins_forever(store: ProjectStore) -> None:
    first = d.freeze_descriptor(store, AST, VER, b'{"a":1}', "projection", "p", "1")
    again = d.freeze_descriptor(store, AST, VER, b'{"a":2}', "published", "p", "2")
    assert again == first and first.descriptor_sha256 == hashlib.sha256(b'{"a":1}').hexdigest()
    rec, raw = d.descriptor(store, AST, VER) or (None, b"")
    assert rec == first and raw == b'{"a":1}'
    assert d.descriptor(store, AST, "ver_0000000000000002") is None


def test_put_delivery_idempotent_and_markers(store: ProjectStore) -> None:
    art = store.register_artifact(b"glb", "model", "model/gltf-binary")
    rec, created = d.put_delivery(store, _fields(), b'{"m":1}', [art.id])
    again, created2 = d.put_delivery(store, _fields(), b'{"m":1}', [art.id])
    assert created and not created2 and rec == again
    assert d.deliveries(store, AST, VER) == [rec]
    assert d.delivery_by_id(store, DLV) == rec and d.delivery_by_id(store, "dlv_0000000000000009") is None
    assert d.manifest_bytes(store, rec) == b'{"m":1}'
    assert d.artifact_marker(store, art.id) == {"asset_id": AST, "version_id": VER, "delivery_id": DLV}
    assert d.artifact_marker(store, "art_0000000000000009") is None


def test_same_id_different_manifest_is_integrity_error(store: ProjectStore) -> None:
    d.put_delivery(store, _fields(), b'{"m":1}', [])
    with pytest.raises(IntegrityError, match="integrity_mismatch"):
        d.put_delivery(store, _fields(), b'{"m":2}', [])


def test_only_projection_keys_are_written(store: ProjectStore) -> None:
    store.create("versions/x.json", {"k": 1})
    d.freeze_descriptor(store, AST, VER, b"{}", "projection", "p", "1")
    d.put_delivery(store, _fields(), b"{}", [])
    keys, _ = store.repo.list_keys("")
    top = {k.split("/")[0] for k in keys}
    assert top <= {"versions", "descriptors", "deliveries", "delivery_index", "artifacts", "blobs"}
    assert store.repo.read_object("versions/x.json").data.startswith(b"{")
