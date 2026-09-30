"""Operator identity and roles (docs/modular/compute-runner.md R11, R16).

Local mode (profile S): the local operator is owner. Proxy mode (profile P): Traefik + Authelia authenticate and
set `Remote-User`/`Remote-Groups`; Studio trusts them only with the proxy shared secret, so a request that reaches
Studio around the proxy cannot claim an identity. Mutating routes are classified in ROLE_RULES; a mutating route
that matches no rule needs `owner`.
"""
from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from typing import Literal

from fastapi import Request

from .settings import ROLES, Settings

Role = Literal["viewer", "reviewer", "owner"]
SECRET_HEADER = "x-assetstudio-proxy-secret"
LOCAL_NAME = "local"
_RANK = {r: i for i, r in enumerate(ROLES)}


@dataclass(frozen=True)
class Operator:
    name: str
    role: Role


class AuthError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# (method or "*" for any mutating method, path regex, minimum role); first match wins.
_P = r"^/api/v[12]/projects/[^/]+"
_GATES = ("edit-prompts|confirm-and-generate|confirm-prompts|mark-regenerate|regenerate|approve-candidates|"
          "clear-approval|preview-best|build-approved|accept-builds|publish")
_JOB_ACTIONS = "enhance|reexport|run-transform|retry-preview|run|cancel|plan|start"
_ITEM_ACTIONS = "add-reference|update-reference|remove-reference|set-preset"
ROLE_RULES: list[tuple[str, str, Role]] = [
    ("GET", r"^/api/v1/audit$", "owner"),
    # Review gates and wave control: what a reviewer does once work exists.
    ("POST", rf"{_P}/(?:batches|jobs|runs)/[^/:]+:(?:{_GATES})$", "reviewer"),
    ("POST", rf"{_P}/runs/[^/:]+:[a-z0-9-]+$", "reviewer"),  # pause | resume | cancel | close
    ("POST", rf"{_P}/variant-plans/[^/:]+:compare-selection$", "reviewer"),
    # Creating and configuring work.
    ("*", r"^/api/v1/projects(?::register)?$", "owner"),
    ("*", r"^/api/v1/projects/[^/]+/(?:config(?::validate)?|storage:(?:test|rebuild-index))$", "owner"),
    ("*", r"^/api/v1/runtime/lanes/[^/:]+:reset$", "owner"),
    ("*", r"^/api/v[12]/(?:operations|tasks)/[^/:]+:(?:cancel|retry)$", "owner"),
    ("*", rf"{_P}/(?:families|assets)/[^/:]+(?::set-current)?$", "owner"),
    ("*", rf"{_P}/(?:imports:(?:preview|preview-set|commit)|shot-list(?::preview-import|:commit-import)?|"
          r"references:upload|media:upload)$", "owner"),
    ("*", rf"{_P}/media/[^/:]+(?::archive|:restore)?$", "owner"),
    ("*", rf"{_P}/(?:batches|jobs)(?:/[^/:]+(?::(?:{_JOB_ACTIONS}))?)?$", "owner"),
    ("*", rf"{_P}/jobs/[^/]+/items/[^/:]+:(?:{_ITEM_ACTIONS})$", "owner"),
    ("*", rf"{_P}/variant-drafts(?:/[^/:]+(?::[a-z-]+)?)?$", "owner"),
    # Runner fleet administration.
    ("*", r"^/api/v1/runner-groups(?:/[^/]+/registration-tokens)?$", "owner"),
    ("*", r"^/api/v1/runners/[^/:]+(?::revoke|/push-url)$", "owner"),
    ("*", r"^/api/v1/attempts/[^/:]+:declare-lost$", "owner"),
]
_COMPILED = [(m, re.compile(p), role) for m, p, role in ROLE_RULES]
_SAFE = ("GET", "HEAD", "OPTIONS")


def match_rule(method: str, path: str) -> Role | None:
    for m, rx, role in _COMPILED:
        if (m == "*" and method not in _SAFE or m == method) and rx.match(path):
            return role
    return None


def required_role(method: str, path: str) -> Role:
    rule = match_rule(method, path)
    if rule is not None:
        return rule
    return "viewer" if method in _SAFE else "owner"


def proxy_trusted(request: Request, settings: Settings) -> bool:
    """True only in proxy mode with the correct shared secret; constant-time compare."""
    if settings.auth_mode != "proxy":
        return False
    given = request.headers.get(SECRET_HEADER, "")
    return bool(given) and hmac.compare_digest(given.encode(), settings.proxy_secret.encode())


def resolve_operator(request: Request, settings: Settings) -> Operator:
    if settings.auth_mode == "local":
        return Operator(LOCAL_NAME, "owner")
    if not proxy_trusted(request, settings):
        raise AuthError(401, "unauthorized", "missing or invalid proxy credential")
    name = request.headers.get(settings.proxy_user_header, "").strip()
    if not name:
        raise AuthError(401, "unauthorized", "missing operator identity")
    groups = {g.strip() for g in request.headers.get(settings.proxy_groups_header, "").split(",") if g.strip()}
    granted = [role for role, group in settings.role_groups.items() if group in groups]
    if not granted:
        raise AuthError(403, "forbidden", "operator belongs to no Studio role group")
    return Operator(name, max(granted, key=_RANK.__getitem__))  # type: ignore[arg-type]


def authorize(request: Request, settings: Settings) -> Operator:
    op = resolve_operator(request, settings)
    need = required_role(request.method, request.url.path)
    if _RANK[op.role] < _RANK[need]:
        raise AuthError(403, "forbidden", f"{need} role required")
    return op
