"""Runner registration, authentication, sessions and heartbeats (R3, R4, R6)."""
from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from assetstudio_core.canonical import canonical_json
from pydantic import Field, StringConstraints, field_validator, model_validator

from .base import AttemptId, GroupId, Msg, PublicKeyB64, RunnerId, SessionId, Sha256, SignatureB64, Timestamp
from .execution import AttemptReport, AttemptState, DispositionReceipt, FileRef

Name64 = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class Platform(Msg):
    os: Name64
    arch: Name64
    hostname: Name64


class StudioKey(Msg):
    key_id: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    public_key: PublicKeyB64
    status: Literal["current", "next"]


class RegisterRequest(Msg):
    schema_: Literal["assetstudio.runner.register.v1"] = Field(alias="schema")
    registration_token: Annotated[str, StringConstraints(min_length=16, max_length=256)]
    public_key: PublicKeyB64
    name: Name64
    platform: Platform


class RegisterResponse(Msg):
    runner_id: RunnerId
    group_id: GroupId
    ephemeral: bool
    studio_keys: list[StudioKey] = Field(min_length=1)


class Challenge(Msg):
    nonce: Annotated[str, StringConstraints(min_length=32, max_length=128)]
    expires_at: Timestamp
    audience: str


class TokenRequest(Msg):
    runner_id: RunnerId
    nonce: Annotated[str, StringConstraints(min_length=32, max_length=128)]
    signature: SignatureB64


class AccessToken(Msg):
    access_token: str
    expires_at: Timestamp


def challenge_message(runner_id: str, nonce: str, audience: str) -> bytes:
    return canonical_json({"aud": audience, "nonce": nonce, "runner_id": runner_id,
                           "schema": "assetstudio.runner.challenge.v1"})


class LocalAttempt(Msg):
    attempt_id: AttemptId
    generation: int = Field(ge=1)
    state: AttemptState
    spooled: list[FileRef] = []


class SessionHello(Msg):
    schema_: Literal["assetstudio.runner.session.v1"] = Field(alias="schema")
    runner_id: RunnerId
    boot_id: str
    protocol_versions: list[int] = Field(min_length=1)
    software: dict[str, str] = Field(max_length=32)
    platform: Platform
    dispatch: Literal["pull", "push"]
    local_attempts: list[LocalAttempt] = Field(default=[], max_length=1000)

    @field_validator("boot_id")
    @classmethod
    def _uuid4(cls, v: str) -> str:
        if uuid.UUID(v).version != 4:
            raise ValueError("boot_id must be a uuid4")
        return v


class SessionAccepted(Msg):
    session_id: SessionId
    protocol_version: int
    catalog_sha256: Sha256
    catalog: dict[str, Any]
    studio_keys: list[StudioKey]
    heartbeat_s: int = Field(ge=5, le=300)
    lease_s: int = Field(ge=15, le=3600)
    offer_ttl_s: int = Field(ge=5, le=600)

    @model_validator(mode="after")
    def _lease_covers_heartbeats(self) -> SessionAccepted:
        if self.lease_s <= 2 * self.heartbeat_s:
            raise ValueError("lease_s must exceed 2 * heartbeat_s")
        return self


class Control(Msg):
    attempt_id: AttemptId
    generation: int = Field(ge=1)
    control: Literal["run", "cancel"]


class Heartbeat(Msg):
    session_id: SessionId
    inventory_revision: int = Field(ge=0)
    attempts: list[AttemptReport] = Field(default=[], max_length=256)
    lifecycle: Literal["active", "draining", "custody_transferred", "safe_to_terminate"] = "active"


class HeartbeatResponse(Msg):
    controls: list[Control] = []
    receipts: list[DispositionReceipt] = []
    lease_until: Timestamp
    inventory_wanted: bool = False
