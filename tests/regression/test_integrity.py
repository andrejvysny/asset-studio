"""RI13/RI14 (H07): content-addressed blobs are verified on reuse, read, approval, publication; no symlink escape."""
from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from assetstudio_core.kinds import Kind, Origin
from assetstudio_storage.local import LocalBackend
from assetstudio_storage.project import ProjectStore
from assetstudio_storage.publication import NewAsset, PublishRequest, publish
from assetstudio_storage.repo import Conflict, CorruptBlob, IntegrityError


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    (tmp_path / "p").mkdir()
    return ProjectStore(LocalBackend(tmp_path / "p"), "prj_0000000000000000")


def _corrupt_same_size(store: ProjectStore, sha: str) -> None:
    p = store.repo._blob_path(sha)  # type: ignore[attr-defined]
    data = bytearray(p.read_bytes())
    data[0] ^= 0xFF
    os.chmod(p, 0o644)
    p.write_bytes(bytes(data))


def test_same_size_corrupt_blob_rejected_on_dedup_reuse(store: ProjectStore) -> None:
    ref = store.repo.write_blob(io.BytesIO(b"original bytes"))
    _corrupt_same_size(store, ref.sha256)
    with pytest.raises(CorruptBlob):
        store.repo.write_blob(io.BytesIO(b"original bytes"))
    # never silently overwritten: the damaged file is left for an explicit repair
    assert store.repo._blob_path(ref.sha256).read_bytes() != b"original bytes"  # type: ignore[attr-defined]


def test_corrupt_blob_rejected_on_read_and_publication(store: ProjectStore) -> None:
    art = store.register_artifact(b"PNGDATA-1234", "image", "image/png")
    assert store.artifact_bytes(art.id) == b"PNGDATA-1234"
    _corrupt_same_size(store, art.sha256)
    with pytest.raises(CorruptBlob):
        store.artifact_bytes(art.id)
    with pytest.raises(IntegrityError):
        publish(store, PublishRequest(op_id="op1", idempotency_key="k" * 8, artifacts={"image": art.id},
                                      preview_role=None, origin=Origin.imported,
                                      new_asset=NewAsset("rock", "Rock", Kind.concept_art, Origin.imported, None)))


def test_blob_symlink_escape_refused(store: ProjectStore, tmp_path: Path) -> None:
    ref = store.repo.write_blob(io.BytesIO(b"abc"))
    outside = tmp_path / "outside"
    outside.mkdir()
    shard = store.repo._blob_path(ref.sha256).parent  # type: ignore[attr-defined]
    for f in shard.iterdir():
        os.chmod(f, 0o644)
        (outside / f.name).write_bytes(f.read_bytes())
        f.unlink()
    shard.rmdir()
    shard.symlink_to(outside)
    with pytest.raises(IntegrityError):
        store.repo.read_blob_verified(ref.sha256)


def test_derived_artifact_registration_is_replay_safe(store: ProjectStore) -> None:
    a = store.register_artifact(b"x1", "image", "image/png", artifact_id="art_0000000000000001")
    b = store.register_artifact(b"x1", "image", "image/png", artifact_id="art_0000000000000001")
    assert a.id == b.id and a.created_at == b.created_at
    with pytest.raises(Conflict):
        store.register_artifact(b"other", "image", "image/png", artifact_id="art_0000000000000001")
