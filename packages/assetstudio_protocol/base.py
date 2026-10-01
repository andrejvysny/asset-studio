"""Shared message base and constrained scalar types."""
from __future__ import annotations

import base64
import binascii
from typing import Annotated

from assetstudio_core.canonical import SHA256_RE_PATTERN
from assetstudio_core.ids import is_id
from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints


class Msg(BaseModel):
    # `schema` is a wire field name but shadows BaseModel.schema, so models expose it as `schema_` with alias "schema";
    # serialize_by_alias keeps the wire key stable for model_dump/canonical signing.
    model_config = ConfigDict(extra="forbid", frozen=True, serialize_by_alias=True, validate_by_name=True,
                              validate_by_alias=True)


def _id_of(prefix: str):
    def check(v: str) -> str:
        if not is_id(v, prefix):
            raise ValueError(f"expected a {prefix}_ id, got {v!r}")
        return v

    return check


def _b64_len(n: int):
    def check(v: str) -> str:
        try:
            raw = base64.b64decode(v, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValueError("not valid base64") from e
        if len(raw) != n:
            raise ValueError(f"base64 must decode to exactly {n} bytes, got {len(raw)}")
        return v

    return check


Sha256 = Annotated[str, StringConstraints(pattern=SHA256_RE_PATTERN)]
RunnerId = Annotated[str, AfterValidator(_id_of("rnr"))]
GroupId = Annotated[str, AfterValidator(_id_of("rgp"))]
SessionId = Annotated[str, AfterValidator(_id_of("rse"))]
AttemptId = Annotated[str, AfterValidator(_id_of("atp"))]
UploadId = Annotated[str, AfterValidator(_id_of("xfr"))]
TaskId = Annotated[str, StringConstraints(min_length=1, max_length=64)]
CallKey = Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:/-]+$")]
Timestamp = Annotated[str, StringConstraints(max_length=40)]
Label = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.-]{0,62}$")]
# Ed25519 public key (32 bytes) and signature (64 bytes), standard base64.
PublicKeyB64 = Annotated[str, AfterValidator(_b64_len(32))]
SignatureB64 = Annotated[str, AfterValidator(_b64_len(64))]
