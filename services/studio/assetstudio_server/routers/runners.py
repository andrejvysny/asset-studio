"""Operator API for compute runners and their groups (R3, R6). Behind the same CSRF gate as the browser API;
there is no bearer auth here (profile P fronts it with forward-auth). Audit actor is "operator" until roles land."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from ..errors import ApiError
from ..services import attempts
from ..services._runner_util import fmt, now_dt
from ..services.placement import session_fresh
from ..studio import Studio
from .deps import studio

router = APIRouter(prefix="/api/v1")
ACTOR = "operator"
DETAIL_LIMIT = 50


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionSummary(_Out):
    id: str
    dispatch: str
    lifecycle: str
    last_seen_at: str
    fresh: bool
    protocol_version: int


class DeviceSummary(_Out):
    uuid: str
    index: int
    name: str
    claim: str
    claim_attempt: str | None
    fallback: bool


class EngineSummary(_Out):
    engine: str
    operations: list[str]


class SlotSummary(_Out):
    slot_id: str
    capability: str
    device_uuids: list[str]
    engines: list[EngineSummary]
    state: str
    loaded_residency: str | None


class RunnerSummary(_Out):
    id: str
    name: str
    group_id: str
    state: str
    platform: dict[str, Any]
    created_at: str
    last_seen_at: str | None
    push_url: str | None
    session: SessionSummary | None
    devices: list[DeviceSummary]
    slots: list[SlotSummary]
    attempt_counts: dict[str, int]


class AttemptSummary(_Out):
    id: str
    task_id: str
    call_key: str
    generation: int
    operation: str
    state: str
    control: str
    disposition: str | None
    updated_at: str
    placement_reasons: list[str]
    runner_id: str | None = None


class AuditRow(_Out):
    seq: int
    at: str
    actor: str
    event: str
    runner_id: str | None
    detail: dict[str, Any]


class RunnerDetail(RunnerSummary):
    attempts: list[AttemptSummary]
    audit: list[AuditRow]


class RunnerList(_Out):
    runners: list[RunnerSummary]


class GroupOut(_Out):
    id: str
    name: str
    projects: list[str] | Literal["*"]
    operations: list[str] | Literal["*"]
    labels: list[str]
    ephemeral: bool
    created_at: str
    created_by: str


class GroupList(_Out):
    groups: list[GroupOut]


class GroupCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    projects: list[str] | Literal["*"]
    operations: list[str] | Literal["*"]
    labels: list[str] = []
    ephemeral: bool = False


class TokenCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ttl_s: int = Field(default=900, ge=60, le=3600)


class TokenOut(_Out):
    token: str
    expires_at: str
    group_id: str


class PushUrl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str | None


def _summary(s: Studio, runner: dict[str, Any], rows: list[dict[str, Any]]) -> RunnerSummary:
    session = s.journal.runners.active_session(runner["id"])
    return RunnerSummary(
        **{k: runner[k] for k in ("id", "name", "group_id", "state", "platform", "created_at", "last_seen_at",
                                  "push_url")},
        session=SessionSummary(fresh=session_fresh(s, session), **{
            k: session[k] for k in ("id", "dispatch", "lifecycle", "last_seen_at", "protocol_version")
        }) if session else None,
        devices=[DeviceSummary(index=d["idx"], **{k: d[k] for k in ("uuid", "name", "claim", "claim_attempt",
                                                                    "fallback")})
                 for d in s.journal.runners.devices(runner["id"])],
        slots=[SlotSummary(engines=[EngineSummary(engine=e["engine"], operations=[o["op"] for o in e["operations"]])
                                    for e in sl["engines"]],
                           **{k: sl[k] for k in ("slot_id", "capability", "device_uuids", "state",
                                                 "loaded_residency")})
               for sl in s.journal.runners.slots(runner["id"])],
        attempt_counts=dict(Counter(a["state"] for a in rows)))


def _attempt(a: dict[str, Any]) -> AttemptSummary:
    return AttemptSummary(
        placement_reasons=a["progress"].get("placement", {}).get("reasons", []),
        **{k: a[k] for k in ("id", "task_id", "call_key", "generation", "operation", "state", "control",
                             "disposition", "updated_at", "runner_id")})


def _runner_attempts(s: Studio, runner_id: str) -> list[dict[str, Any]]:
    return s.journal.attempts.list(runner_id=runner_id, limit=100_000)


def _known_runner(s: Studio, runner_id: str) -> dict[str, Any]:
    runner = s.auth.get_runner(runner_id)
    if runner is None:
        raise ApiError(404, "not_found", f"unknown runner {runner_id}")
    return runner


@router.get("/runners", response_model=RunnerList)
def list_runners(s: Studio = Depends(studio)) -> RunnerList:
    return RunnerList(runners=[_summary(s, r, _runner_attempts(s, r["id"])) for r in s.auth.runners()])


@router.get("/runners/{runner_id}", response_model=RunnerDetail)
def get_runner(runner_id: str, s: Studio = Depends(studio)) -> RunnerDetail:
    runner = _known_runner(s, runner_id)
    rows = _runner_attempts(s, runner_id)
    return RunnerDetail(
        **_summary(s, runner, rows).model_dump(),
        attempts=[_attempt(a) for a in reversed(rows[-DETAIL_LIMIT:])],
        audit=[AuditRow(**r) for r in s.auth.audit_log(limit=DETAIL_LIMIT, runner_id=runner_id)])


@router.get("/runner-groups", response_model=GroupList)
def list_groups(s: Studio = Depends(studio)) -> GroupList:
    return GroupList(groups=[GroupOut(**g) for g in s.auth.groups()])


@router.post("/runner-groups", response_model=GroupOut)
def create_group(body: GroupCreate, s: Studio = Depends(studio)) -> GroupOut:
    try:
        group = s.auth.create_group(body.name, body.projects, body.operations, body.labels, body.ephemeral, ACTOR)
    except ValueError as e:
        raise ApiError(409, "conflict", str(e)) from e
    return GroupOut(**group)


@router.post("/runner-groups/{group_id}/registration-tokens", response_model=TokenOut)
def create_token(group_id: str, body: TokenCreate, s: Studio = Depends(studio)) -> TokenOut:
    expires = fmt(now_dt() + timedelta(seconds=body.ttl_s))  # the store stamps its own (equal to within ms)
    try:
        token = s.auth.create_registration_token(group_id, body.ttl_s, ACTOR)
    except KeyError as e:
        raise ApiError(404, "not_found", f"unknown runner group {group_id}") from e
    return TokenOut(token=token, expires_at=expires, group_id=group_id)


@router.post("/runners/{runner_id}:revoke", response_model=RunnerSummary)
def revoke(runner_id: str, s: Studio = Depends(studio)) -> RunnerSummary:
    _known_runner(s, runner_id)
    s.auth.revoke_runner(runner_id, ACTOR)
    return _summary(s, _known_runner(s, runner_id), _runner_attempts(s, runner_id))


def _valid_push_url(url: str) -> bool:
    try:
        u = urlsplit(url)
        return u.scheme in ("http", "https") and bool(u.hostname) and "@" not in u.netloc and not u.fragment \
            and "#" not in url
    except ValueError:
        return False


@router.put("/runners/{runner_id}/push-url", response_model=RunnerSummary)
def set_push_url(runner_id: str, body: PushUrl, s: Studio = Depends(studio)) -> RunnerSummary:
    _known_runner(s, runner_id)
    if body.url is not None and not _valid_push_url(body.url):
        raise ApiError(422, "invalid_input", "push url must be http(s) without userinfo or fragment")
    s.auth.set_push_url(runner_id, body.url)
    s.auth.audit("push_url_set", ACTOR, runner_id, {"url": body.url})
    return _summary(s, _known_runner(s, runner_id), _runner_attempts(s, runner_id))


@router.post("/attempts/{attempt_id}:declare-lost", response_model=AttemptSummary)
def declare_lost(attempt_id: str, s: Studio = Depends(studio)) -> AttemptSummary:
    if s.journal.attempts.get(attempt_id) is None:
        raise ApiError(404, "not_found", f"unknown attempt {attempt_id}")
    if not attempts.declare_lost(s, attempt_id, actor=ACTOR):
        raise ApiError(409, "conflict", "only an uncertain attempt can be declared lost")
    return _attempt(s.journal.attempts.get(attempt_id))  # type: ignore[arg-type]
