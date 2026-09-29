"""Persisted records. Immutable records are written once; mutable ones carry a revision for optimistic updates."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .kinds import Kind, Origin

SCHEMA_VERSION = 1
TaskState = Literal[
    "queued", "running", "succeeded", "failed", "cancel_requested", "cancelled", "blocked", "reconciling"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = SCHEMA_VERSION


class Artifact(Record):
    id: str
    role: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    mime: str
    meta: dict[str, Any] = {}
    lineage: list[str] = []
    created_at: str
    retention: Literal["essential", "candidate", "raw", "log"] = "essential"
    source: dict[str, Any] = {}


class VersionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_id: str
    display_version: int
    published_at: str
    publication_op: str
    preview_artifact_id: str | None = None
    note: str = ""


class PointerChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_version: str | None
    to_version: str
    at: str
    op: str
    reason: str = ""


class AssetManifest(Record):
    asset_id: str
    name_id: str
    display_name: str
    kind: Kind
    origin: Origin
    category_id: str | None
    tags: list[str] = []
    created_at: str
    current_version_id: str | None = None
    versions: list[VersionRef] = []
    pointer_log: list[PointerChange] = []
    revision: int = 1

    def version(self, version_id: str) -> VersionRef | None:
        return next((v for v in self.versions if v.version_id == version_id), None)


class AssetVersion(Record):
    asset_id: str
    version_id: str
    display_version: int
    project_id: str
    kind: Kind
    origin: Origin
    display_name: str
    category_id: str | None
    tags: list[str] = []
    artifacts: dict[str, dict[str, Any]]  # role -> {artifact_id, sha256, size, mime}
    sources: dict[str, Any] = {}
    config_snapshot_sha: str | None = None
    models: list[dict[str, Any]] = []
    engine: dict[str, Any] = {}
    parameters: dict[str, Any] = {}
    qa: dict[str, Any] | None = None
    validation: dict[str, Any] = {}
    licence: dict[str, Any] = {}
    publication: dict[str, Any]
    note: str = ""


class ShotItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str = Field(min_length=1, max_length=200)
    category_id: str | None = None
    kind: Kind | None = None
    brief: str = Field(default="", max_length=4000)
    priority: Literal["low", "med", "high"] = "med"
    notes: str = Field(default="", max_length=4000)
    target_asset_id: str | None = None
    external_id: str | None = None
    source: dict[str, Any] = {}
    archived: bool = False


class ShotList(Record):
    revision: int = 0
    items: list[ShotItem] = []


class Batch(Record):
    id: str
    alias: str
    title: str
    kind: Kind
    recipe_id: str
    category_id: str | None
    created_at: str
    seed_family: int
    item_ids: list[str]
    source: str = "ad hoc"
    config_revision: int
    revision: int = 1


class TaskRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op_id: str
    state: TaskState
    error: str | None = None
    progress: dict[str, Any] = {}


class Published(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str
    version_id: str
    display_version: int


class BatchItem(Record):
    id: str
    batch_id: str
    name: str
    brief: str
    category_id: str | None
    shot_id: str | None = None
    target_asset_id: str | None = None
    snapshot_sha: str
    revision: int = 1
    prompt_revisions: list[str] = []
    current_prompt: str | None = None
    prompt_confirmed: str | None = None
    candidate_sets: list[str] = []
    current_set: str | None = None
    qa: dict[str, str] = {}  # candidate id -> qa evaluation id (current set)
    decisions: list[str] = []
    approval: str | None = None
    regen_requested: bool = False
    build_runs: list[str] = []
    current_build: str | None = None
    accepted_build: str | None = None
    published: Published | None = None
    cancelled: bool = False
    tasks: dict[str, TaskRef] = {}
    created_at: str
    updated_at: str


class PromptRevision(Record):
    id: str
    item_id: str
    number: int
    parent_id: str | None = None
    created_at: str
    origin: Literal["enhanced", "edited", "brief"]
    original_brief: str
    enhancer: dict[str, Any] | None = None
    description: str
    template: str
    positive: str
    negative: str
    style_sha: str | None = None
    snapshot_sha: str


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    index: int
    artifact_id: str
    sha256: str
    seed: int
    width: int
    height: int
    engine: dict[str, Any] = {}


class CandidateSet(Record):
    id: str
    item_id: str
    number: int
    prompt_revision_id: str
    created_at: str
    op_id: str
    requested: int
    candidates: list[Candidate]
    generation: dict[str, Any] = {}


class QaEvaluation(Record):
    id: str
    item_id: str
    candidate_set_id: str
    candidate_id: str
    image_sha256: str
    ruleset: dict[str, Any] | None
    ruleset_sha: str | None
    results: list[dict[str, Any]]
    policy: dict[str, Any]
    evaluators: dict[str, Any] = {}
    evaluated_at: str
    op_id: str | None = None


class ReviewDecision(Record):
    id: str
    gate: Literal["candidate_approval", "final_acceptance"]
    batch_id: str
    item_id: str
    bound: dict[str, Any]
    qa_status: str | None = None
    override_qa: bool = False
    override_reason: str | None = None
    failed_checks: list[str] = []
    missing_checks: list[str] = []
    decided_at: str
    actor: str = "operator"
    idempotency_key: str


class BuildRun(Record):
    id: str
    item_id: str
    batch_id: str
    build: str
    inputs: dict[str, Any]
    status: TaskState
    result: Literal["valid", "invalid", "validation_unavailable"] | None = None
    artifacts: dict[str, str] = {}
    validation: dict[str, Any] = {}
    created_at: str
    updated_at: str
    op_id: str
    error: str | None = None
