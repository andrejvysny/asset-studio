"""Offers, attempts and results (R5, R8). Capability/Engine live here so inventory can import this module."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from assetstudio_core.canonical import canonical_json, sha256_json
from pydantic import Field, StringConstraints, field_validator, model_validator

from .base import AttemptId, CallKey, Label, Msg, RunnerId, SessionId, Sha256, TaskId, Timestamp
from .errors import ErrorBody

Capability = Literal["image", "aux3d"]
Engine = Literal["comfyui", "aux", "worker3d"]

Operation = Literal[
    "image.t2i", "image.edit", "aux.enhance", "aux.compare", "aux.qa", "aux.cutout", "aux.analyze_source",
    "aux.suggest_variants", "worker3d.generate", "worker3d.export",
]
OPERATION_VERSIONS: dict[str, int] = {
    "image.t2i": 1, "image.edit": 1, "aux.enhance": 1, "aux.compare": 1, "aux.qa": 1, "aux.cutout": 1,
    "aux.analyze_source": 1, "aux.suggest_variants": 1, "worker3d.generate": 1, "worker3d.export": 1,
}
OPERATION_CAPABILITY: dict[str, Capability] = {
    "image.t2i": "image", "image.edit": "image", "aux.enhance": "aux3d", "aux.compare": "aux3d", "aux.qa": "aux3d",
    "aux.cutout": "aux3d", "aux.analyze_source": "aux3d", "aux.suggest_variants": "aux3d",
    "worker3d.generate": "aux3d", "worker3d.export": "aux3d",
}
OPERATION_ENGINE: dict[str, Engine] = {
    "image.t2i": "comfyui", "image.edit": "comfyui", "aux.enhance": "aux", "aux.compare": "aux", "aux.qa": "aux",
    "aux.cutout": "aux", "aux.analyze_source": "aux", "aux.suggest_variants": "aux",
    "worker3d.generate": "worker3d", "worker3d.export": "worker3d",
}

AttemptState = Literal[
    "offered", "leased", "admitted", "executing", "spooled", "uploading", "ingested", "committed", "failed", "lost",
    "cancelled", "uncertain", "quarantined",
]
TERMINAL_STATES = frozenset({"committed", "failed", "lost", "cancelled", "quarantined"})
Disposition = Literal["committed", "rejected", "cancelled", "quarantined"]


class FileRef(Msg):
    name: Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.-]+$")]
    sha256: Sha256
    size: int = Field(ge=0)
    mime: str


class InputRef(Msg):
    sha256: Sha256
    size: int = Field(ge=0)
    role: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    label: Annotated[str, StringConstraints(max_length=64)] = ""
    mime: str


class Requirements(Msg):
    capability: Capability
    engine: Engine
    models: list[str] = []
    resource_profile: str | None = None
    labels: list[Label] = []


class Policy(Msg):
    allow_model_substitution: Literal[False] = False
    allow_runtime_downloads: Literal[False] = False
    retry: Literal["bounded"] = "bounded"


def compute_input_digest(operation: str, operation_version: int, inputs: list[InputRef], params: dict[str, Any],
                         requirements: Requirements, policy: Policy) -> str:
    """Placement-independent identity: attempt/runner/session ids are deliberately excluded. Input order is kept."""
    return sha256_json({
        "operation": operation, "operation_version": operation_version,
        "inputs": [i.model_dump() for i in inputs], "params": params,
        "requirements": requirements.model_dump(), "policy": policy.model_dump(),
    })


class Offer(Msg):
    schema_: Literal["assetstudio.execution.v1"] = Field(alias="schema")
    attempt_id: AttemptId
    task_id: TaskId
    call_key: CallKey
    generation: int = Field(ge=1)
    runner_id: RunnerId
    session_id: SessionId
    slot_id: Label
    operation: Operation
    operation_version: int
    input_digest: Sha256
    inputs: list[InputRef] = Field(max_length=64)
    params: dict[str, Any]
    requirements: Requirements
    policy: Policy = Policy()
    offer_expires_at: Timestamp
    signature: str | None = None

    @model_validator(mode="after")
    def _check_identity(self) -> Offer:
        if self.operation_version != OPERATION_VERSIONS[self.operation]:
            raise ValueError(f"operation_version {self.operation_version} unsupported for {self.operation}")
        if self.requirements.capability != OPERATION_CAPABILITY[self.operation]:
            raise ValueError(f"capability {self.requirements.capability} does not match {self.operation}")
        expected = compute_input_digest(self.operation, self.operation_version, self.inputs, self.params,
                                        self.requirements, self.policy)
        if self.input_digest != expected:
            raise ValueError("input_digest does not match offer contents")
        return self


def offer_signing_bytes(offer: Offer) -> bytes:
    return canonical_json(offer.model_dump(exclude={"signature"}))


class AcceptRequest(Msg):
    session_id: SessionId
    generation: int = Field(ge=1)


class AcceptResponse(Msg):
    attempt_id: AttemptId
    generation: int = Field(ge=1)
    start_authorized: bool
    lease_until: Timestamp


class RejectRequest(Msg):
    session_id: SessionId
    generation: int = Field(ge=1)
    reason: Literal["admission_busy", "missing_model", "resource_exhausted", "unsupported", "other"]
    detail: Annotated[str, StringConstraints(max_length=500)] = ""


class AttemptReport(Msg):
    attempt_id: AttemptId
    generation: int = Field(ge=1)
    state: AttemptState
    progress: dict[str, Any] = {}
    error: ErrorBody | None = None


class ResultManifest(Msg):
    schema_: Literal["assetstudio.result.v1"] = Field(alias="schema")
    attempt_id: AttemptId
    generation: int = Field(ge=1)
    files: list[FileRef] = Field(max_length=64)
    meta: dict[str, Any] = {}

    @field_validator("files")
    @classmethod
    def _unique_names(cls, v: list[FileRef]) -> list[FileRef]:
        names = [f.name for f in v]
        if len(set(names)) != len(names):
            raise ValueError("file names must be unique")
        return v


class DispositionReceipt(Msg):
    attempt_id: AttemptId
    generation: int = Field(ge=1)
    disposition: Disposition
    at: Timestamp
