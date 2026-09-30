"""Persisted records. Immutable records are written once; mutable ones carry a revision for optimistic updates."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .kinds import Kind, Origin

# 2: production "Batch" became "Job" (records read v1 `batch_id` as `job_id`); true Batches group Jobs.
SCHEMA_VERSION = 2
TaskState = Literal[
    "queued", "running", "succeeded", "failed", "cancel_requested", "cancelled", "blocked", "reconciling"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = SCHEMA_VERSION


class JobScoped(Record):
    """v1 records name their Job `batch_id` (the entity was called Batch). Read-compatible; never rewritten when
    immutable, so published provenance keeps its original bytes and hashes."""

    @model_validator(mode="before")
    @classmethod
    def _v1_batch_id(cls, data: Any) -> Any:
        if isinstance(data, dict) and "batch_id" in data and "job_id" not in data:
            data = {**data, "job_id": data["batch_id"], "schema_version": SCHEMA_VERSION}
            data.pop("batch_id")
        return data


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
    family_id: str | None = None  # the ONLY family-membership authority (at most one family per asset)

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
    derivation: dict[str, Any] | None = None  # variants.Derivation (immutable lineage of a variant version)


class AssetFamily(Record):
    """A project-local, same-kind group of independent assets. Holds metadata + the anchor it started from;
    membership is AssetManifest.family_id (no member list here that could drift)."""

    id: str
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    kind: Kind
    anchor_asset_id: str
    anchor_version_id: str
    created_at: str
    updated_at: str
    revision: int = 1
    created_by_op: str


class MediaItem(Record):
    """A project media-library image: guidance-only brainstorm reference, never a production asset. Identity is the
    content sha256 (id derived from it), so re-uploading the same bytes is the same item."""

    id: str
    artifact_id: str
    sha256: str
    thumb_artifact_id: str
    name: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default=[], max_length=20)
    source_rights: str = Field(default="unknown", max_length=200)
    source_url: str = Field(default="", max_length=500)
    format: str
    width: int
    height: int
    size: int
    has_alpha: bool
    created_at: str
    updated_at: str
    archived_at: str | None = None
    revision: int = 1


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


class Job(Record):
    """A configured production workflow for one or more items of one kind/recipe (formerly "Batch").
    Legacy ids keep their `bat_` prefix; new Jobs use `job_`."""

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
    archived_at: str | None = None
    variant: dict[str, Any] | None = None  # variants.VariantContext when created by New variant / Create variants
    direct: bool = False  # direct transform: no prompt, candidates or approval; one deterministic result


class Batch(Record):
    """A named group of Jobs of one project, scheduled together. Owns no item content, history or style."""

    id: str
    alias: str
    title: str
    job_ids: list[str] = []
    policy: dict[str, Any] = {}
    created_at: str
    updated_at: str
    revision: int = 1
    runs: list[str] = []
    archived_at: str | None = None


RunStatus = Literal["planned", "running", "waiting_for_review", "paused", "completed", "completed_with_errors",
                    "cancelled", "closed"]
Gate = Literal["prompt_confirmation", "candidate_approval", "build", "final_acceptance", "publication"]


class BatchRun(Record):
    """Frozen execution selection of a Batch (or of one Job when `batch_id` is None: a standalone run)."""

    id: str
    batch_id: str | None
    batch_revision: int | None
    plan_id: str
    plan_sha256: str
    command_id: str
    selection: dict[str, dict[str, int]]  # job id -> {item id: item revision at start}
    stop_at: str
    created_at: str
    waves: list[str] = []
    closed_at: str | None = None
    close_reason: str | None = None


class WaveSelection(Record):
    """One human gate action across Jobs: the exact revisions it bound. Each unit also has its own decision."""

    id: str
    run_id: str | None
    gate: Gate
    command_id: str
    units: list[dict[str, Any]]
    created_at: str
    actor: str = "operator"


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
    build_run_id: str | None = None  # the accepted run this publication committed (None: recorded before variants)


class JobItem(JobScoped):
    """One requested output. `tasks`/`cancelled` are legacy (v1) fields kept for history: live task state is in
    the journal only (a single authority avoids the item/journal dual-write crash gap)."""

    id: str
    job_id: str
    name: str
    brief: str
    category_id: str | None
    shot_id: str | None = None
    target_asset_id: str | None = None
    snapshot_sha: str
    references: list[dict[str, Any]] = []  # JobReference: {id, artifact_id, sha256, origin, note, crop, label}
    references_revision: int = 0  # bumps on every reference change (prompt confirmation binds it)
    enhance_preset: Literal["conservative", "creative"] = "conservative"
    revision: int = 1
    prompt_revisions: list[str] = []
    current_prompt: str | None = None
    prompt_confirmed: str | None = None
    candidate_sets: list[str] = []
    current_set: str | None = None
    qa: dict[str, str] = {}  # candidate id -> qa evaluation id (current set)
    qa_history: dict[str, dict[str, str]] = {}  # earlier rounds: candidate set id -> {candidate id -> qa id}
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
    # What this revision was written against (variant plan/reference set, item references revision, preset,
    # edit mode, enhancer facts/additions). Confirmation binds the whole revision, so these travel with it.
    bindings: dict[str, Any] = {}


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


class ReviewDecision(JobScoped):
    id: str
    gate: Literal["candidate_approval", "final_acceptance", "transform_confirmation"]
    job_id: str
    item_id: str
    run_id: str | None = None
    wave_id: str | None = None
    bound: dict[str, Any]
    qa_status: str | None = None
    override_qa: bool = False
    override_reason: str | None = None
    failed_checks: list[str] = []
    missing_checks: list[str] = []
    decided_at: str
    actor: str = "operator"
    idempotency_key: str


class Checkpoint(BaseModel):
    """A committed build stage: enough to resume the next stage without redoing this one."""

    model_config = ConfigDict(extra="forbid")
    stage: str
    stage_version: int = 1
    inputs: dict[str, Any] = {}  # input artifact ids + sha256
    settings: dict[str, Any] = {}
    identities: dict[str, Any] = {}  # actual model/engine/implementation identities at execution
    outputs: dict[str, str] = {}  # role -> artifact id
    receipt: dict[str, Any] = {}
    committed_at: str


class BuildRun(JobScoped):
    """One build attempt or explicit derivative (re-export/repair). Attached to its item when CREATED, so a
    failed attempt and its durable intermediates stay visible and recoverable."""

    id: str
    item_id: str
    job_id: str
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
    kind: Literal["build", "reexport", "repair"] = "build"
    derived_from: str | None = None  # source BuildRun of a re-export/repair
    checkpoints: dict[str, Checkpoint] = {}
    executions: dict[str, list[str]] = {}  # stage -> worker execution ids, persisted BEFORE each submission
    preview: Literal["pending", "available", "failed", "unsupported"] | None = None
    preview_error: str | None = None
