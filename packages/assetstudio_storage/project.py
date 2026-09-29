"""Typed access to one project's logical layout (spec §13.1) over any Repository."""
from __future__ import annotations

import io
import json
import threading
from typing import Any, BinaryIO, TypeVar

from assetstudio_core.canonical import canonical_json, now_iso, pretty_json
from assetstudio_core.config import StudioConfig, parse_config
from assetstudio_core.domain import Artifact, ShotList
from assetstudio_core.ids import new_id
from assetstudio_core.inheritance import validate_semantics
from assetstudio_core.safeyaml import ParseError, dump_yaml, load_yaml
from pydantic import BaseModel

from .repo import BlobRef, Conflict, NotFound, Repository, StorageError

M = TypeVar("M", bound=BaseModel)
CONFIG_KEY = "studio.yaml"
SHOTLIST_KEY = "shotlist.yaml"


class InvalidProjectData(StorageError):
    code = "invalid_project_data"

    def __init__(self, key: str, errors: list[Any]) -> None:
        super().__init__(f"{key} is invalid: {errors[:3]}")
        self.key = key
        self.errors = errors


def manifest_key(asset_id: str) -> str:
    return f"manifests/{asset_id}.json"


def version_key(asset_id: str, version_id: str) -> str:
    return f"versions/{asset_id}/{version_id}.json"


def artifact_key(artifact_id: str) -> str:
    return f"artifacts/{artifact_id}.json"


def snapshot_key(sha: str) -> str:
    return f"config/revisions/{sha}.json"


def batch_key(batch_id: str, *rest: str) -> str:
    return "/".join(("batches", batch_id, *rest))


class ProjectStore:
    def __init__(self, repo: Repository, project_id: str) -> None:
        self.repo = repo
        self.project_id = project_id
        # Serializes read-modify-write sequences in this process; tokens still guard every replace.
        self.lock = threading.RLock()

    # --- generic JSON records ------------------------------------------------------------------------------------
    def get(self, key: str, model: type[M]) -> tuple[M, str]:
        obj = self.repo.read_object(key)
        try:
            return model.model_validate_json(obj.data), obj.token
        except ValueError as e:
            raise InvalidProjectData(key, [str(e)[:300]]) from e

    def get_opt(self, key: str, model: type[M]) -> tuple[M | None, str | None]:
        try:
            return self.get(key, model)
        except NotFound:
            return None, None

    def create(self, key: str, record: BaseModel | dict) -> str:
        data = record.model_dump(mode="json") if isinstance(record, BaseModel) else record
        return self.repo.create_if_absent(key, pretty_json(data))

    def replace(self, key: str, record: BaseModel | dict, token: str) -> str:
        data = record.model_dump(mode="json") if isinstance(record, BaseModel) else record
        return self.repo.replace_if_version(key, token, pretty_json(data))

    def create_or_same(self, key: str, record: BaseModel | dict) -> None:
        """Idempotent immutable write: an identical existing record is fine, a different one is a conflict."""
        data = record.model_dump(mode="json") if isinstance(record, BaseModel) else record
        try:
            self.repo.create_if_absent(key, pretty_json(data))
        except Conflict:
            existing = json.loads(self.repo.read_object(key).data)
            if canonical_json(existing) != canonical_json(data):
                raise
        return None

    def list_ids(self, prefix: str, suffix: str = ".json") -> list[str]:
        out: list[str] = []
        cursor: str | None = None
        while True:
            keys, cursor = self.repo.list_keys(prefix, cursor)
            out += [k.rsplit("/", 1)[-1].removesuffix(suffix) for k in keys if k.endswith(suffix)]
            if cursor is None:
                return out

    # --- configuration -------------------------------------------------------------------------------------------
    def read_config(self) -> tuple[StudioConfig, str]:
        obj = self.repo.read_object(CONFIG_KEY)
        try:
            data = load_yaml(obj.data)
        except ParseError as e:
            raise InvalidProjectData(CONFIG_KEY, [{"path": f"line {e.line}", "message": str(e)}]) from e
        cfg, errors = parse_config(data)
        if cfg is None:
            raise InvalidProjectData(CONFIG_KEY, [e.model_dump() for e in errors])
        sem = validate_semantics(cfg)
        if sem:
            raise InvalidProjectData(CONFIG_KEY, [e.model_dump() for e in sem])
        return cfg, obj.token

    @staticmethod
    def config_text(cfg: StudioConfig) -> str:
        return dump_yaml(cfg.model_dump(mode="json", exclude_defaults=False))

    def write_config(self, cfg: StudioConfig, token: str | None) -> str:
        data = self.config_text(cfg).encode()
        if token is None:
            return self.repo.create_if_absent(CONFIG_KEY, data)
        return self.repo.replace_if_version(CONFIG_KEY, token, data)

    def save_snapshot(self, snapshot: dict[str, Any]) -> str:
        sha = snapshot["sha256"]
        self.create_or_same(snapshot_key(sha), snapshot)
        return sha

    def read_snapshot(self, sha: str) -> dict[str, Any]:
        return json.loads(self.repo.read_object(snapshot_key(sha)).data)

    # --- shot list ---------------------------------------------------------------------------------------------
    def read_shotlist(self) -> tuple[ShotList, str | None]:
        try:
            obj = self.repo.read_object(SHOTLIST_KEY)
        except NotFound:
            return ShotList(), None
        try:
            return ShotList.model_validate(load_yaml(obj.data) or {}), obj.token
        except (ParseError, ValueError) as e:
            raise InvalidProjectData(SHOTLIST_KEY, [str(e)[:300]]) from e

    def write_shotlist(self, shots: ShotList, token: str | None) -> str:
        data = dump_yaml(shots.model_dump(mode="json")).encode()
        if token is None:
            return self.repo.create_if_absent(SHOTLIST_KEY, data)
        return self.repo.replace_if_version(SHOTLIST_KEY, token, data)

    # --- artifacts -------------------------------------------------------------------------------------------------
    def register_artifact(self, content: bytes | BinaryIO, role: str, mime: str, *, meta: dict | None = None,
                          lineage: list[str] | None = None, retention: str = "essential",
                          source: dict | None = None, expected_sha256: str | None = None) -> Artifact:
        stream = io.BytesIO(content) if isinstance(content, bytes) else content
        ref: BlobRef = self.repo.write_blob(stream, expected_sha256)
        art = Artifact(id=new_id("art"), role=role, sha256=ref.sha256, size=ref.size, mime=mime, meta=meta or {},
                       lineage=lineage or [], created_at=now_iso(), retention=retention,  # type: ignore[arg-type]
                       source=source or {})
        self.create(artifact_key(art.id), art)
        return art

    def artifact(self, artifact_id: str) -> Artifact:
        return self.get(artifact_key(artifact_id), Artifact)[0]

    def artifact_bytes(self, artifact_id: str) -> bytes:
        art = self.artifact(artifact_id)
        with self.repo.open_blob(art.sha256) as f:
            return f.read()
