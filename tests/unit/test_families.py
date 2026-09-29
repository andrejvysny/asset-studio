"""Families: storage records, membership, publication, index filter/grouping. (Variants milestone, Phase A)"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from assetstudio_core.domain import AssetManifest, AssetVersion
from assetstudio_core.ids import derived_id
from assetstudio_core.kinds import Kind, Origin
from assetstudio_storage.families import (
    FamilyConflict,
    FamilyKindMismatch,
    attach_asset,
    create_family,
    family_key,
    get_family,
    list_families,
    update_family,
)
from assetstudio_storage.index import AssetIndex
from assetstudio_storage.local import LocalBackend
from assetstudio_storage.project import ProjectStore, manifest_key, version_key
from assetstudio_storage.publication import NewAsset, PublishRequest, publish
from assetstudio_storage.repo import Conflict, NotFound

FID = derived_id("fam", "prj", "op-fam")


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    (tmp_path / "proj").mkdir()
    return ProjectStore(LocalBackend(tmp_path / "proj"), "prj_0000000000000000")


@pytest.fixture
def index(tmp_path: Path) -> AssetIndex:
    return AssetIndex(tmp_path / "idx" / "i.sqlite")


def _pub(store: ProjectStore, op: str, name: str, kind: Kind = Kind.concept_art, family_id: str | None = None,
         derivation: dict | None = None) -> tuple[str, str]:
    role = "model" if kind is Kind.model3d else "image"
    art = store.register_artifact(op.encode(), role, "image/png")
    r = publish(store, PublishRequest(
        op_id=op, idempotency_key=op, artifacts={role: art.id}, preview_role=None, origin=Origin.imported,
        new_asset=NewAsset(name.lower().replace(" ", "-"), name, kind, Origin.imported, None, family_id=family_id),
        derivation=derivation))
    return r.asset_id, r.version_id


def _family(store: ProjectStore, anchor: tuple[str, str], kind: Kind = Kind.concept_art, op: str = "op-fam",
            name: str = "Fam") -> str:
    fid = derived_id("fam", "prj", op)
    create_family(store, family_id=fid, name=name, kind=kind, anchor_asset_id=anchor[0],
                  anchor_version_id=anchor[1], op_id=op)
    return fid


def _index_all(store: ProjectStore, index: AssetIndex) -> None:
    index.rebuild(store)


def test_create_family_replay_and_conflict(store: ProjectStore) -> None:
    a = _pub(store, "o1", "Anchor")
    f1 = create_family(store, family_id=FID, name="F", kind=Kind.concept_art, anchor_asset_id=a[0],
                       anchor_version_id=a[1], op_id="op-fam")
    assert create_family(store, family_id=FID, name="F", kind=Kind.concept_art, anchor_asset_id=a[0],
                         anchor_version_id=a[1], op_id="op-fam") == f1
    with pytest.raises(Conflict):
        create_family(store, family_id=FID, name="F", kind=Kind.concept_art, anchor_asset_id=a[0],
                      anchor_version_id=a[1], op_id="other-op")
    assert [f.id for f in list_families(store)] == [FID]


def test_attach_asset_rules(store: ProjectStore) -> None:
    a, b = _pub(store, "o1", "A"), _pub(store, "o2", "B")
    fid, other = _family(store, a), _family(store, a, op="op-2", name="Two")
    m = attach_asset(store, asset_id=a[0], family_id=fid, expected_manifest_revision=1)
    assert m.family_id == fid and m.revision == 2 and len(m.versions) == 1
    assert attach_asset(store, asset_id=a[0], family_id=fid, expected_manifest_revision=1).revision == 2  # no-op
    with pytest.raises(FamilyConflict):
        attach_asset(store, asset_id=a[0], family_id=other, expected_manifest_revision=None)
    with pytest.raises(FamilyConflict):  # stale source revision while membership would change
        attach_asset(store, asset_id=b[0], family_id=fid, expected_manifest_revision=99)
    assert store.get(manifest_key(b[0]), AssetManifest)[0].family_id is None
    model = _pub(store, "o3", "Mesh", Kind.model3d)
    with pytest.raises(FamilyKindMismatch):
        attach_asset(store, asset_id=model[0], family_id=fid, expected_manifest_revision=None)
    with pytest.raises(NotFound):
        attach_asset(store, asset_id=b[0], family_id=derived_id("fam", "x", "y"), expected_manifest_revision=None)


def test_publish_joins_family_with_derivation(store: ProjectStore) -> None:
    a = _pub(store, "o1", "A")
    fid = _family(store, a)
    deriv = {"method": "image_edit", "source": {"asset_id": a[0], "version_id": a[1]}}
    v = _pub(store, "o2", "Variant", family_id=fid, derivation=deriv)
    m = store.get(manifest_key(v[0]), AssetManifest)[0]
    assert m.family_id == fid
    assert store.get(version_key(*v), AssetVersion)[0].derivation == deriv
    assert store.get(version_key(*a), AssetVersion)[0].derivation is None


def test_publish_unknown_family_writes_nothing(store: ProjectStore) -> None:
    art = store.register_artifact(b"x", "image", "image/png")
    req = PublishRequest(op_id="o", idempotency_key="o", artifacts={"image": art.id}, preview_role=None,
                         origin=Origin.imported,
                         new_asset=NewAsset("n", "N", Kind.concept_art, Origin.imported, None,
                                            family_id=derived_id("fam", "x", "nope")))
    with pytest.raises(NotFound):
        publish(store, req)
    for prefix in ("manifests", "versions", "names", "publications"):
        assert store.repo.list_keys(prefix)[0] == []


def test_publish_kind_mismatch_before_writes(store: ProjectStore) -> None:
    a = _pub(store, "o1", "A")
    fid = _family(store, a)
    before = {p: len(store.repo.list_keys(p)[0]) for p in ("manifests", "versions", "names", "publications")}
    art = store.register_artifact(b"glb", "model", "model/gltf-binary")
    req = PublishRequest(op_id="o9", idempotency_key="o9", artifacts={"model": art.id}, preview_role=None,
                         origin=Origin.imported,
                         new_asset=NewAsset("m", "M", Kind.model3d, Origin.imported, None, family_id=fid))
    with pytest.raises(FamilyKindMismatch):
        publish(store, req)
    assert before == {p: len(store.repo.list_keys(p)[0]) for p in before}


def test_update_family_revision(store: ProjectStore) -> None:
    fid = _family(store, _pub(store, "o1", "A"))
    f = update_family(store, fid, 1, name="Renamed", description="d")
    assert (f.name, f.revision) == ("Renamed", 2) and get_family(store, fid).description == "d"
    with pytest.raises(Conflict):
        update_family(store, fid, 1, name="X")
    with pytest.raises(ValueError):
        update_family(store, fid, 2, name="  ")


def _seed(store: ProjectStore, index: AssetIndex) -> str:
    a = _pub(store, "o1", "Alpha wall")
    fid = _family(store, a, name="Walls")
    attach_asset(store, asset_id=a[0], family_id=fid, expected_manifest_revision=None)
    _pub(store, "o2", "Beta wall", family_id=fid)
    _pub(store, "o3", "Gamma banner", family_id=fid)
    _pub(store, "o4", "Delta solo")
    _index_all(store, index)
    return fid


def test_index_family_filter(store: ProjectStore, index: AssetIndex) -> None:
    fid = _seed(store, index)
    rows, total = index.query(family_id=fid)
    assert total == 3 and {r["family_name"] for r in rows} == {"Walls"}
    assert index.query(q="walls")[1] == 3  # family name is searchable
    assert index.query(family_id=derived_id("fam", "x", "y"))[1] == 0


def test_grouped_filter_first(store: ProjectStore, index: AssetIndex) -> None:
    fid = _seed(store, index)
    out = index.query_grouped(q="banner", limit=10)
    (g,) = out["groups"]
    assert g["type"] == "family" and g["matching_count"] == 1 and g["total_member_count"] == 3
    assert g["representative_asset_id"] == g["member_preview"][0]["asset_id"]
    assert g["member_preview"][0]["display_name"] == "Gamma banner" and g["family_id"] == fid
    full = index.query_grouped(limit=10)
    assert full["matching_asset_count"] == 4 and full["matching_group_count"] == 2
    assert [x["type"] for x in full["groups"]] == ["family", "asset"]  # Alpha wall < Delta solo
    assert full["groups"][0]["representative_asset_id"] == index.query(family_id=fid)[0][0]["asset_id"]


def test_grouped_family_across_pages_once_and_stale_cursor(store: ProjectStore, index: AssetIndex) -> None:
    a = _pub(store, "o0", "Member 00")
    fid = _family(store, a)
    attach_asset(store, asset_id=a[0], family_id=fid, expected_manifest_revision=None)
    for i in range(1, 9):
        _pub(store, f"o{i}", f"Member {i:02d}", family_id=fid)
    for i in range(5):
        _pub(store, f"s{i}", f"Solo {i}")
    _index_all(store, index)
    seen: list[str] = []
    cursor = None
    while True:
        out = index.query_grouped(limit=2, cursor=cursor)
        seen += [g.get("family_id") or g["asset"]["asset_id"] for g in out["groups"]]
        cursor = out["next_cursor"]
        if cursor is None:
            break
    assert seen.count(fid) == 1 and len(seen) == 6 == out["matching_group_count"]
    assert len(out["groups"]) <= 2 and out["matching_asset_count"] == 14
    first = index.query_grouped(limit=2)
    assert len(first["groups"][0]["member_preview"]) == 4
    with pytest.raises(ValueError, match="stale_cursor"):
        index.query_grouped(q="solo", limit=2, cursor=first["next_cursor"])
    index.upsert(store.get(manifest_key(a[0]), AssetManifest)[0])
    with pytest.raises(ValueError, match="stale_cursor"):
        index.query_grouped(limit=2, cursor=first["next_cursor"])
    with pytest.raises(ValueError, match="stale_cursor"):
        index.query_grouped(limit=2, cursor="garbage")


def test_upsert_keeps_family_name_and_rebuild_recovers(store: ProjectStore, index: AssetIndex) -> None:
    fid = _seed(store, index)
    aid = index.member_ids(fid)[0]
    index.upsert(store.get(manifest_key(aid), AssetManifest)[0])  # metadata edit path passes no name
    assert index.query(family_id=fid)[1] == 3 and index.query(q="walls")[1] == 3
    index._db.execute("DELETE FROM assets")
    index.rebuild(store)
    assert index.query(family_id=fid)[1] == 3 and index.family_member_counts() == {fid: 3}


def test_old_index_migrated_and_flagged(tmp_path: Path, store: ProjectStore) -> None:
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE assets (asset_id TEXT PRIMARY KEY, name_id TEXT NOT NULL, display_name TEXT NOT NULL,
      kind TEXT NOT NULL, origin TEXT NOT NULL, category_id TEXT, tags TEXT NOT NULL, current_version_id TEXT,
      display_version INTEGER, version_count INTEGER NOT NULL, preview_artifact_id TEXT, updated_at TEXT NOT NULL,
      search TEXT NOT NULL)""")
    db.commit()
    db.close()
    fid = _seed(store, AssetIndex(tmp_path / "scratch.sqlite"))
    idx = AssetIndex(path)
    assert idx.needs_rebuild()
    idx.rebuild(store)
    assert not idx.needs_rebuild() and idx.query(family_id=fid)[1] == 3
    assert not AssetIndex(tmp_path / "fresh.sqlite").needs_rebuild()
    assert family_key(fid) == f"families/{fid}.json"
