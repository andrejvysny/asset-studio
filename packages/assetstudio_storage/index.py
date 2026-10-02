"""Derived, rebuildable asset search index (SQLite in the instance dir, never inside the portable project)."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.domain import AssetFamily, AssetManifest

from .families import family_key
from .project import ProjectStore, manifest_key

_COLUMNS = ("asset_id", "name_id", "display_name", "kind", "origin", "category_id", "tags", "current_version_id",
            "display_version", "version_count", "preview_artifact_id", "updated_at", "search", "family_id",
            "family_name", "archived")
_DDL = """CREATE TABLE IF NOT EXISTS {t} (
  asset_id TEXT PRIMARY KEY, name_id TEXT NOT NULL, display_name TEXT NOT NULL, kind TEXT NOT NULL,
  origin TEXT NOT NULL, category_id TEXT, tags TEXT NOT NULL, current_version_id TEXT, display_version INTEGER,
  version_count INTEGER NOT NULL, preview_artifact_id TEXT, updated_at TEXT NOT NULL, search TEXT NOT NULL,
  family_id TEXT, family_name TEXT, archived INTEGER NOT NULL DEFAULT 0)"""
_PLACEHOLDERS = ",".join("?" * len(_COLUMNS))
PREVIEW_MEMBERS = 4


def _row(m: AssetManifest, family_name: str | None = None) -> tuple[Any, ...]:
    cur = m.version(m.current_version_id) if m.current_version_id else None
    search = " ".join([m.asset_id, m.name_id, m.display_name, *m.tags, family_name or ""]).lower()
    return (m.asset_id, m.name_id, m.display_name, m.kind.value, m.origin.value, m.category_id, json.dumps(m.tags),
            m.current_version_id, cur.display_version if cur else None, len(m.versions),
            cur.preview_artifact_id if cur else None, now_iso(), search, m.family_id,
            family_name if m.family_id else None, 1 if m.archived_at else 0)


_ORDER = "display_name COLLATE NOCASE, asset_id"


def _out(r: sqlite3.Row) -> dict[str, Any]:
    d = {k: r[k] for k in r.keys() if k not in ("rn", "matching_count")}
    return {**d, "tags": json.loads(r["tags"])}


def _filter(categories: set[str] | None, kind: str | None, origin: str | None, q: str | None,
            family_id: str | None, tags: list[str] | None = None,
            archived: bool | None = False) -> tuple[str, list[Any]]:
    """`archived`: False = active assets only (the default everywhere), True = archived only, None = both."""
    where: list[str] = ["current_version_id IS NOT NULL"]
    args: list[Any] = []
    if archived is not None:
        where.append("archived = ?")
        args.append(1 if archived else 0)
    if categories is not None:
        where.append(f"category_id IN ({','.join('?' * len(categories))})")
        args += sorted(categories)
    for col, val in (("kind", kind), ("origin", origin), ("family_id", family_id)):
        if val:
            where.append(f"{col} = ?")
            args.append(val)
    if q:
        where.append("search LIKE ? ESCAPE '\\'")
        args.append("%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    for tag in tags or []:  # AND semantics
        where.append("EXISTS (SELECT 1 FROM json_each(assets.tags) WHERE value = ?)")
        args.append(tag)
    return " AND ".join(where), args


def _encode_cursor(offset: int, qhash: str, rev: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"offset": offset, "qhash": qhash, "rev": rev}).encode()).decode()


def _decode_cursor(cursor: str | None, qhash: str, rev: int) -> int:
    if not cursor:
        return 0
    try:
        c = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        offset = int(c["offset"])
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError("stale_cursor") from e
    if c.get("qhash") != qhash or c.get("rev") != rev or offset < 0:
        raise ValueError("stale_cursor")
    return offset


class AssetIndex:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._rebuild_lock = threading.Lock()
        self._pending: dict[str, tuple[Any, ...] | None] | None = None  # upserts (None = delete) landing mid-rebuild
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(_DDL.format(t="assets"))
        self._db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(assets)")}
        if "family_id" not in cols:  # index from before families: add columns, memberships need a rebuild
            self._db.execute("ALTER TABLE assets ADD COLUMN family_id TEXT")
            self._db.execute("ALTER TABLE assets ADD COLUMN family_name TEXT")
            self._db.execute("INSERT OR REPLACE INTO meta VALUES ('needs_rebuild', '1')")
        if "archived" not in cols:  # manifests without archived_at are active: the default 0 is already correct
            self._db.execute("ALTER TABLE assets ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        self._db.close()

    def _bump(self) -> None:
        """Monotonic change counter: grouped cursors bind to it so a changed index cannot silently reshuffle pages."""
        self._db.execute("INSERT INTO meta VALUES ('revision', '1') ON CONFLICT(key) DO UPDATE "
                         "SET value = CAST(value AS INTEGER) + 1")

    def _revision_locked(self) -> int:
        r = self._db.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
        return int(r[0]) if r else 0

    def revision(self) -> int:
        with self._lock:
            return self._revision_locked()

    def needs_rebuild(self) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM meta WHERE key = 'needs_rebuild'").fetchone() is not None

    def upsert(self, manifest: AssetManifest, family_name: str | None = None) -> None:
        with self._lock:
            if family_name is None and manifest.family_id:  # metadata edits must not drop the denormalised name
                r = self._db.execute("SELECT family_name FROM assets WHERE asset_id = ? AND family_id = ?",
                                     (manifest.asset_id, manifest.family_id)).fetchone()
                family_name = r[0] if r else None
            row = _row(manifest, family_name)
            self._db.execute(f"INSERT OR REPLACE INTO assets ({','.join(_COLUMNS)}) VALUES ({_PLACEHOLDERS})", row)
            if self._pending is not None:
                self._pending[manifest.asset_id] = row
            self._bump()

    def delete(self, asset_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM assets WHERE asset_id = ?", (asset_id,))
            if self._pending is not None:  # a rebuild is collecting: its snapshot may still hold the row
                self._pending[asset_id] = None
            self._bump()

    def _collect(self, store: ProjectStore) -> tuple[list[tuple[Any, ...]], list[str]]:
        names: dict[str, str] = {}
        for fid in store.list_ids("families"):
            fam, _ = store.get_opt(family_key(fid), AssetFamily)
            if fam is not None:
                names[fid] = fam.name
        rows: list[tuple[Any, ...]] = []
        failed: list[str] = []
        for asset_id in store.list_ids("manifests"):
            try:
                m = store.get(manifest_key(asset_id), AssetManifest)[0]
                rows.append(_row(m, names.get(m.family_id or "")))
            except Exception:  # a corrupt manifest is reported, not silently indexed
                failed.append(asset_id)
        return rows, failed

    def _swap(self, rows: list[tuple[Any, ...]]) -> None:
        """Caller holds self._lock. Upserts recorded during collection supersede the (older) snapshot rows."""
        insert = f"INSERT OR REPLACE INTO assets_new ({','.join(_COLUMNS)}) VALUES ({_PLACEHOLDERS})"
        self._db.execute("DROP TABLE IF EXISTS assets_new")
        self._db.execute(_DDL.format(t="assets_new"))
        self._db.executemany(insert, rows)
        gone = [a for a, r in (self._pending or {}).items() if r is None]
        self._db.executemany(insert, [r for r in (self._pending or {}).values() if r is not None])
        if gone:
            self._db.executemany("DELETE FROM assets_new WHERE asset_id = ?", [(a,) for a in gone])
        self._db.execute("BEGIN")
        try:
            self._db.execute("DROP TABLE assets")
            self._db.execute("ALTER TABLE assets_new RENAME TO assets")
            self._db.execute("INSERT OR REPLACE INTO meta VALUES ('rebuilt_at', ?)", (now_iso(),))
            self._db.execute("DELETE FROM meta WHERE key = 'needs_rebuild'")
            self._bump()
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def rebuild(self, store: ProjectStore) -> dict[str, Any]:
        """Shadow table + swap: queries keep answering from the old table until the new one is complete.
        Upserts during the (unlocked) collection are recorded and re-applied at swap, so none is lost."""
        with self._rebuild_lock:
            with self._lock:
                self._pending = {}
            try:
                rows, failed = self._collect(store)
                with self._lock:
                    self._swap(rows)
            finally:
                with self._lock:
                    self._pending = None
        return {"indexed": len(rows), "errors": len(failed), "error_ids": failed[:100]}

    def count(self, archived: bool | None = False) -> int:
        clause, args = _filter(None, None, None, None, None, archived=archived)
        with self._lock:
            return int(self._db.execute(f"SELECT COUNT(*) FROM assets WHERE {clause}", args).fetchone()[0])

    def query(self, *, categories: set[str] | None = None, kind: str | None = None, origin: str | None = None,
              q: str | None = None, family_id: str | None = None, limit: int = 60,
              offset: int = 0, tags: list[str] | None = None,
              archived: bool | None = False) -> tuple[list[dict[str, Any]], int]:
        if categories is not None and not categories:
            return [], 0
        rows, total, _ = self.query_at_revision(categories=categories, kind=kind, origin=origin, q=q,
                                                family_id=family_id, limit=limit, offset=offset, tags=tags,
                                                archived=archived)
        return rows, total

    def query_at_revision(self, *, categories: set[str] | None = None, kind: str | None = None,
                          origin: str | None = None, q: str | None = None, family_id: str | None = None,
                          limit: int = 60, offset: int = 0, tags: list[str] | None = None,
                          archived: bool | None = False) -> tuple[list[dict[str, Any]], int, int]:
        """(rows, total, revision) read under one lock: a page cursor names exactly the snapshot it came from."""
        if categories is not None and not categories:
            return [], 0, self.revision()
        clause, args = _filter(categories, kind, origin, q, family_id, tags, archived)
        with self._lock:
            rev = self._revision_locked()
            total = int(self._db.execute(f"SELECT COUNT(*) FROM assets WHERE {clause}", args).fetchone()[0])
            rows = self._db.execute(
                f"SELECT * FROM assets WHERE {clause} ORDER BY {_ORDER} LIMIT ? OFFSET ?",
                [*args, limit, offset]).fetchall()
        return [_out(r) for r in rows], total, rev

    def query_grouped(self, *, categories: set[str] | None = None, kind: str | None = None,
                      origin: str | None = None, q: str | None = None, family_id: str | None = None,
                      limit: int = 60, cursor: str | None = None, archived: bool | None = False) -> dict[str, Any]:
        """Filter first, then group: units are families (matching members only) or single ungrouped assets,
        ordered by their best matching member and paginated as units, so a family never splits across pages."""
        qhash = hashlib.sha256(json.dumps([sorted(categories) if categories is not None else None, kind, origin, q,
                                           family_id, archived]).encode()).hexdigest()[:16]
        clause, args = _filter(categories, kind, origin, q, family_id, archived=archived)
        unit = "COALESCE(family_id, 'a:' || asset_id)"
        with self._lock:  # one critical section: the cursor revision must belong to the page it accompanies
            rev = self._revision_locked()
            offset = _decode_cursor(cursor, qhash, rev)
            empty = {"group_by": "family", "matching_asset_count": 0, "matching_group_count": 0,
                     "query_revision": rev, "groups": [], "next_cursor": None}
            if categories is not None and not categories:
                return empty
            n_assets, n_groups = self._db.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT {unit}) FROM assets WHERE {clause}", args).fetchone()
            heads = self._db.execute(
                f"SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY {unit} ORDER BY {_ORDER}) AS rn, "
                f"COUNT(*) OVER (PARTITION BY {unit}) AS matching_count FROM assets WHERE {clause}) "
                f"WHERE rn = 1 ORDER BY {_ORDER} LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
            fids = sorted({r["family_id"] for r in heads if r["family_id"]})
            previews, totals = self._family_details(clause, args, fids)
        groups: list[dict[str, Any]] = []
        for r in heads:
            fid = r["family_id"]
            if not fid:
                groups.append({"type": "asset", "asset": _out(r)})
                continue
            groups.append({"type": "family", "family_id": fid, "name": r["family_name"],
                           "matching_count": r["matching_count"], "total_member_count": totals.get(fid, 0),
                           "representative_asset_id": r["asset_id"], "member_preview": previews.get(fid, []),
                           "members_cursor": None})
        nxt = offset + len(heads)
        return {**empty, "matching_asset_count": n_assets, "matching_group_count": n_groups, "groups": groups,
                "next_cursor": _encode_cursor(nxt, qhash, rev) if nxt < n_groups else None}

    def _family_details(self, clause: str, args: list[Any], fids: list[str]
                        ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
        if not fids:
            return {}, {}
        marks = ",".join("?" * len(fids))
        rows = self._db.execute(
            f"SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY family_id ORDER BY {_ORDER}) AS rn "
            f"FROM assets WHERE {clause} AND family_id IN ({marks})) WHERE rn <= {PREVIEW_MEMBERS} ORDER BY {_ORDER}",
            [*args, *fids]).fetchall()
        previews: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            previews.setdefault(r["family_id"], []).append(_out(r))
        return previews, self._member_counts(fids)

    def _member_counts(self, fids: list[str]) -> dict[str, int]:
        marks = ",".join("?" * len(fids))
        return {r[0]: int(r[1]) for r in self._db.execute(
            f"SELECT family_id, COUNT(*) FROM assets WHERE current_version_id IS NOT NULL AND archived = 0 "
            f"AND family_id IN ({marks}) "
            "GROUP BY family_id", fids)}

    def family_member_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT family_id, COUNT(*) FROM assets WHERE current_version_id IS NOT NULL "
                                    "AND archived = 0 AND family_id IS NOT NULL GROUP BY family_id").fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def member_ids(self, family_id: str) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute(
                "SELECT asset_id FROM assets WHERE family_id = ? ORDER BY asset_id", (family_id,))]

    def counts_by_category(self) -> dict[str | None, int]:
        with self._lock:
            rows = self._db.execute("SELECT category_id, COUNT(*) FROM assets WHERE current_version_id IS NOT NULL "
                                    "AND archived = 0 GROUP BY category_id").fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def name_ids(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self._db.execute("SELECT name_id FROM assets").fetchall()}
