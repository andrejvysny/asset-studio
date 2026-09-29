"""Local backend conformance, publication atomicity/idempotency, writer ownership (S02 S03 S08 P02)."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from assetstudio_core.domain import AssetManifest
from assetstudio_core.ids import derived_id
from assetstudio_core.kinds import Kind, Origin
from assetstudio_storage.local import LocalBackend, WriterLock
from assetstudio_storage.project import ProjectStore, manifest_key, version_key
from assetstudio_storage.publication import NewAsset, PublishRequest, StalePointer, publish, set_current
from assetstudio_storage.repo import Conflict, IntegrityError, ReadOnly, StorageError


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(LocalBackend(tmp_path), "prj_0000000000000000")


def test_conditional_writes(store: ProjectStore) -> None:
    repo = store.repo
    t1 = repo.create_if_absent("a/b.json", b"1")
    with pytest.raises(Conflict):
        repo.create_if_absent("a/b.json", b"2")
    t2 = repo.replace_if_version("a/b.json", t1, b"2")
    with pytest.raises(Conflict):
        repo.replace_if_version("a/b.json", t1, b"3")  # stale token never overwrites
    assert repo.read_object("a/b.json").data == b"2" and repo.read_object("a/b.json").token == t2
    for bad in ("../x", "/etc/passwd", "a/../../x", "a//b", ""):
        with pytest.raises(StorageError):
            repo.read_object(bad)


def test_blobs_dedupe_and_verify(store: ProjectStore) -> None:
    r1 = store.repo.write_blob(io.BytesIO(b"hello"))
    r2 = store.repo.write_blob(io.BytesIO(b"hello"))
    assert r1.sha256 == r2.sha256 and r1.created and not r2.created
    with pytest.raises(IntegrityError):
        store.repo.write_blob(io.BytesIO(b"x"), expected_sha256="0" * 64)
    assert not list((Path(store.repo.root) / "blobs" / ".staging").glob(".tmp-*"))  # type: ignore[attr-defined]


def test_read_only_backend(tmp_path: Path) -> None:
    ro = LocalBackend(tmp_path, read_only=True)
    with pytest.raises(ReadOnly):
        ro.create_if_absent("x", b"1")


def test_second_writer_refused(tmp_path: Path) -> None:
    a, b = WriterLock(tmp_path), WriterLock(tmp_path)
    assert a.try_acquire() and not b.try_acquire()
    a.release()
    assert b.try_acquire()
    b.release()


def _req(store: ProjectStore, op: str, **kw: object) -> PublishRequest:
    art = store.register_artifact(op.encode(), "image", "image/png")
    base = dict(op_id=op, idempotency_key=op, artifacts={"image": art.id}, preview_role=None,
                origin=Origin.imported)
    base.update(kw)
    return PublishRequest(**base)  # type: ignore[arg-type]


def test_publish_idempotent_and_stale_pointer(store: ProjectStore) -> None:
    na = NewAsset("x", "X", Kind.concept_art, Origin.imported, None)
    req = _req(store, "op-1", new_asset=na)
    r1 = publish(store, req)
    assert publish(store, req) == type(r1)(r1.asset_id, r1.version_id, 1, False)
    r2 = publish(store, _req(store, "op-2", asset_id=r1.asset_id, expected_current_version=r1.version_id))
    assert r2.display_version == 2
    with pytest.raises(StalePointer):
        publish(store, _req(store, "op-3", asset_id=r1.asset_id, expected_current_version=r1.version_id))
    m = set_current(store, r1.asset_id, r1.version_id, r2.version_id, "op-4")
    assert m.current_version_id == r1.version_id and len(m.versions) == 2


def test_crash_between_version_and_manifest_resumes(store: ProjectStore) -> None:
    """Version record written, manifest never updated: nothing visible; retry completes with the same ids."""
    first = publish(store, _req(store, "op-a", new_asset=NewAsset("y", "Y", Kind.concept_art, Origin.imported, None)))
    req = _req(store, "op-b", asset_id=first.asset_id, expected_current_version=first.version_id)
    version_id = derived_id("ver", first.asset_id, "op-b")
    manifest, token = store.get(manifest_key(first.asset_id), AssetManifest)
    # simulate the crash: run publish but drop the manifest write
    original = store.replace
    store.replace = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash"))  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        publish(store, req)
    store.replace = original  # type: ignore[assignment]
    assert store.repo.stat_object(version_key(first.asset_id, version_id)) is not None
    assert store.get(manifest_key(first.asset_id), AssetManifest)[0].current_version_id == first.version_id
    res = publish(store, req)
    assert res.version_id == version_id and res.display_version == 2 and res.created


def test_publish_refuses_missing_blob(store: ProjectStore) -> None:
    req = _req(store, "op-z", new_asset=NewAsset("z", "Z", Kind.concept_art, Origin.imported, None))
    art = store.artifact(req.artifacts["image"])
    p = store.repo.blob_path(art.sha256)  # type: ignore[attr-defined]
    p.chmod(0o644)
    p.unlink()
    with pytest.raises(IntegrityError):
        publish(store, req)
    assert store.list_ids("manifests") == []
