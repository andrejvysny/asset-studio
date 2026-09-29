"""Instance project registry: which project roots this Studio serves, and exclusive writer ownership."""
from __future__ import annotations

import json
import os
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from assetstudio_core.canonical import now_iso, pretty_json
from assetstudio_core.config import StudioConfig
from assetstudio_core.defaults import new_project_config
from assetstudio_core.ids import new_id, validate_id
from assetstudio_storage.index import AssetIndex
from assetstudio_storage.local import LocalBackend, WriterLock
from assetstudio_storage.project import ProjectStore
from assetstudio_storage.publication import backfill_names
from assetstudio_storage.repo import NotFound

from .errors import ApiError
from .settings import Settings

OWNER_KEY = "_control/owner.json"


@dataclass
class ProjectContext:
    id: str
    name: str
    root: Path
    store: ProjectStore
    index: AssetIndex
    writer: WriterLock | None
    read_only: bool
    owner: dict[str, Any]

    def config(self) -> tuple[StudioConfig, str]:
        return self.store.read_config()

    def require_writable(self) -> None:
        if self.read_only:
            raise ApiError(409, "read_only",
                           f"project is open read-only: another Studio owns it ({self.owner.get('instance_id')})")


class Registry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.instance_dir / "projects.json"
        self._lock = threading.RLock()
        self._open: dict[str, ProjectContext] = {}

    def _entries(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.path.read_text())["projects"]
        except FileNotFoundError:
            return []

    def _save(self, entries: list[dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(pretty_json({"projects": entries}))
        os.replace(tmp, self.path)

    def _allowed_root(self, root: Path) -> Path:
        root = root.expanduser().resolve()
        if not any(root.is_relative_to(base) for base in self.settings.project_roots):
            allowed = ", ".join(str(b) for b in self.settings.project_roots)
            raise ApiError(422, "root_not_allowed", f"project roots must be inside: {allowed}")
        return root

    def list(self) -> list[dict[str, Any]]:
        out = []
        for e in self._entries():
            ctx = self._open.get(e["id"])
            out.append({**e, "open": ctx is not None, "read_only": ctx.read_only if ctx else None})
        return out

    def get(self, project_id: str) -> ProjectContext:
        validate_id(project_id, "prj")
        with self._lock:
            if project_id in self._open:
                return self._open[project_id]
            entry = next((e for e in self._entries() if e["id"] == project_id), None)
            if entry is None:
                raise ApiError(404, "unknown_project", f"project {project_id} is not registered")
            ctx = self._open_root(Path(entry["root"]), project_id)
            self._open[project_id] = ctx
            return ctx

    def _open_root(self, root: Path, project_id: str) -> ProjectContext:
        writer = WriterLock(root)
        owned = writer.try_acquire()
        backend = LocalBackend(root, read_only=not owned)
        store = ProjectStore(backend, project_id)
        owner: dict[str, Any] = {}
        if owned:
            owner = {"instance_id": self.settings.instance_id, "host": socket.gethostname(), "pid": os.getpid(),
                     "acquired_at": now_iso()}
            try:
                token = backend.read_object(OWNER_KEY).token
                backend.replace_if_version(OWNER_KEY, token, pretty_json(owner))
            except NotFound:
                backend.create_if_absent(OWNER_KEY, pretty_json(owner))
        else:
            try:
                owner = json.loads(backend.read_object(OWNER_KEY).data)
            except NotFound:
                owner = {"instance_id": "unknown"}
        cfg, _ = store.read_config()
        if cfg.project.id != project_id:
            raise ApiError(422, "project_mismatch", f"{root} contains project {cfg.project.id}, not {project_id}")
        if owned:
            backfill_names(store)  # projects from before authoritative name records
        index = AssetIndex(self.settings.instance_dir / "index" / f"{project_id}.sqlite")
        if index.needs_rebuild() or (index.count() == 0 and store.list_ids("manifests")):
            index.rebuild(store)
        return ProjectContext(project_id, cfg.project.name, root, store, index, writer if owned else None,
                              not owned, owner)

    def create(self, name: str, root: Path, starter_qa: bool = True) -> ProjectContext:
        root = self._allowed_root(root)
        with self._lock:
            if root.exists() and any(root.iterdir()):
                raise ApiError(409, "root_not_empty", f"{root} exists and is not empty; use register instead")
            root.mkdir(parents=True, exist_ok=True)
            project_id = new_id("prj")
            store = ProjectStore(LocalBackend(root), project_id)
            store.write_config(new_project_config(project_id, name, starter_qa), None)
            entries = self._entries()
            entries.append({"id": project_id, "name": name, "root": str(root), "backend": "local",
                            "registered_at": now_iso()})
            self._save(entries)
            return self.get(project_id)

    def register(self, root: Path) -> ProjectContext:
        root = self._allowed_root(root)
        with self._lock:
            store = ProjectStore(LocalBackend(root, read_only=True), "prj_0000000000000000")
            cfg, _ = store.read_config()
            entries = self._entries()
            if not any(e["id"] == cfg.project.id for e in entries):
                entries.append({"id": cfg.project.id, "name": cfg.project.name, "root": str(root),
                                "backend": "local", "registered_at": now_iso()})
                self._save(entries)
            return self.get(cfg.project.id)

    def close_all(self) -> None:
        with self._lock:
            for ctx in self._open.values():
                ctx.index.close()
                if ctx.writer:
                    ctx.writer.release()
            self._open.clear()
