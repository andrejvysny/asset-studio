"""R11: index rebuilds never lose concurrent upserts; grouped cursors match the page they came from."""
from __future__ import annotations

import base64
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from assetstudio_core.domain import AssetManifest
from assetstudio_storage.index import AssetIndex
from assetstudio_storage.local import LocalBackend
from assetstudio_storage.project import ProjectStore, manifest_key

from tests.unit.test_families import _pub


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    (tmp_path / "proj").mkdir()
    return ProjectStore(LocalBackend(tmp_path / "proj"), "prj_0000000000000000")


@pytest.fixture
def index(tmp_path: Path) -> AssetIndex:
    return AssetIndex(tmp_path / "idx" / "i.sqlite")


def _manifest(store: ProjectStore, asset_id: str) -> AssetManifest:
    return store.get(manifest_key(asset_id), AssetManifest)[0]


def test_upsert_during_rebuild_is_not_lost(store: ProjectStore, index: AssetIndex,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    old = _pub(store, "o1", "Old one")
    late = _pub(store, "o2", "Late one")
    collected, resume = threading.Event(), threading.Event()
    real = AssetIndex._collect

    def paused(self: AssetIndex, s: ProjectStore) -> Any:
        rows = real(self, s)
        rows[0][:] = [r for r in rows[0] if r[0] != late[0]]  # snapshot predates the late asset
        collected.set()
        assert resume.wait(10)
        return rows

    monkeypatch.setattr(AssetIndex, "_collect", paused)
    out: dict[str, Any] = {}
    t = threading.Thread(target=lambda: out.update(index.rebuild(store)))
    t.start()
    assert collected.wait(10)
    index.upsert(_manifest(store, late[0]))  # lands after collection, before the swap
    resume.set()
    t.join(10)
    ids = {r["asset_id"] for r in index.query()[0]}
    assert ids == {old[0], late[0]} and out["errors"] == 0 and out["error_ids"] == []


def test_rebuild_reports_failed_asset_ids(store: ProjectStore, index: AssetIndex,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    good, bad = _pub(store, "o1", "Good"), _pub(store, "o2", "Bad")
    real = store.get

    def flaky(key: str, *a: Any, **kw: Any) -> Any:
        if key == manifest_key(bad[0]):
            raise ValueError("corrupt")
        return real(key, *a, **kw)

    monkeypatch.setattr(store, "get", flaky)
    res = index.rebuild(store)
    assert res == {"indexed": 1, "errors": 1, "error_ids": [bad[0]]}
    assert [r["asset_id"] for r in index.query()[0]] == [good[0]]


def test_pending_cleared_after_rebuild_failure(store: ProjectStore, index: AssetIndex,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    _pub(store, "o1", "One")

    def boom(self: AssetIndex, s: ProjectStore) -> Any:
        raise RuntimeError("collect failed")

    monkeypatch.setattr(AssetIndex, "_collect", boom)
    with pytest.raises(RuntimeError):
        index.rebuild(store)
    assert index._pending is None


def test_grouped_cursor_revision_matches_page_under_concurrent_upserts(store: ProjectStore,
                                                                        index: AssetIndex) -> None:
    ids = [_pub(store, f"o{i}", f"Asset {i:02d}")[0] for i in range(8)]
    manifests = [_manifest(store, a) for a in ids]
    index.upsert(manifests[0])
    base_rev, base_count = index.revision(), 1
    stop = threading.Event()

    def writer() -> None:
        for m in manifests[1:]:
            index.upsert(m)
        stop.set()

    t = threading.Thread(target=writer)
    t.start()
    seen = 0
    while not stop.is_set() or seen < 20:
        page = index.query_grouped(limit=1)
        rev = page["query_revision"]
        assert page["matching_asset_count"] == base_count + (rev - base_rev)  # data and revision from one snapshot
        if page["next_cursor"]:
            cur = json.loads(base64.urlsafe_b64decode(page["next_cursor"].encode()))
            assert cur["rev"] == rev
        seen += 1
    t.join(10)
