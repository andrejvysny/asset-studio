"""Derived, rebuildable asset search index (SQLite in the instance dir, never inside the portable project)."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.domain import AssetManifest

from .project import ProjectStore, manifest_key

_COLUMNS = ("asset_id", "name_id", "display_name", "kind", "origin", "category_id", "tags", "current_version_id",
            "display_version", "version_count", "preview_artifact_id", "updated_at", "search")
_DDL = """CREATE TABLE IF NOT EXISTS {t} (
  asset_id TEXT PRIMARY KEY, name_id TEXT NOT NULL, display_name TEXT NOT NULL, kind TEXT NOT NULL,
  origin TEXT NOT NULL, category_id TEXT, tags TEXT NOT NULL, current_version_id TEXT, display_version INTEGER,
  version_count INTEGER NOT NULL, preview_artifact_id TEXT, updated_at TEXT NOT NULL, search TEXT NOT NULL)"""


def _row(m: AssetManifest) -> tuple[Any, ...]:
    cur = m.version(m.current_version_id) if m.current_version_id else None
    search = " ".join([m.asset_id, m.name_id, m.display_name, *m.tags]).lower()
    return (m.asset_id, m.name_id, m.display_name, m.kind.value, m.origin.value, m.category_id, json.dumps(m.tags),
            m.current_version_id, cur.display_version if cur else None, len(m.versions),
            cur.preview_artifact_id if cur else None, now_iso(), search)


class AssetIndex:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(_DDL.format(t="assets"))
        self._db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")

    def close(self) -> None:
        self._db.close()

    def upsert(self, manifest: AssetManifest) -> None:
        with self._lock:
            self._db.execute(f"INSERT OR REPLACE INTO assets ({','.join(_COLUMNS)}) VALUES ({','.join('?' * 13)})",
                             _row(manifest))

    def rebuild(self, store: ProjectStore) -> dict[str, int]:
        """Shadow table + swap: queries keep answering from the old table until the new one is complete."""
        ids = store.list_ids("manifests")
        rows, errors = [], 0
        for asset_id in ids:
            try:
                rows.append(_row(store.get(manifest_key(asset_id), AssetManifest)[0]))
            except Exception:  # a corrupt manifest is reported, not silently indexed
                errors += 1
        with self._lock:
            self._db.execute("DROP TABLE IF EXISTS assets_new")
            self._db.execute(_DDL.format(t="assets_new"))
            self._db.executemany(f"INSERT INTO assets_new ({','.join(_COLUMNS)}) VALUES ({','.join('?' * 13)})", rows)
            self._db.execute("BEGIN")
            self._db.execute("DROP TABLE assets")
            self._db.execute("ALTER TABLE assets_new RENAME TO assets")
            self._db.execute("INSERT OR REPLACE INTO meta VALUES ('rebuilt_at', ?)", (now_iso(),))
            self._db.execute("COMMIT")
        return {"indexed": len(rows), "errors": errors}

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM assets").fetchone()[0])

    def query(self, *, categories: set[str] | None = None, kind: str | None = None, origin: str | None = None,
              q: str | None = None, limit: int = 60, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        where, args = ["current_version_id IS NOT NULL"], []
        if categories is not None:
            if not categories:
                return [], 0
            where.append(f"category_id IN ({','.join('?' * len(categories))})")
            args += sorted(categories)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if origin:
            where.append("origin = ?")
            args.append(origin)
        if q:
            where.append("search LIKE ? ESCAPE '\\'")
            args.append("%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        clause = " AND ".join(where)
        with self._lock:
            total = int(self._db.execute(f"SELECT COUNT(*) FROM assets WHERE {clause}", args).fetchone()[0])
            rows = self._db.execute(
                f"SELECT * FROM assets WHERE {clause} ORDER BY display_name COLLATE NOCASE, asset_id LIMIT ? OFFSET ?",
                [*args, limit, offset]).fetchall()
        return [{**dict(r), "tags": json.loads(r["tags"])} for r in rows], total

    def counts_by_category(self) -> dict[str | None, int]:
        with self._lock:
            rows = self._db.execute("SELECT category_id, COUNT(*) FROM assets WHERE current_version_id IS NOT NULL "
                                    "GROUP BY category_id").fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def name_ids(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self._db.execute("SELECT name_id FROM assets").fetchall()}
