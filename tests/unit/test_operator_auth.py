"""Operator roles (R16), proxy trust (R11) and unauthenticated-endpoint limits (R14)."""
from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from assetstudio_client import keys
from assetstudio_server.main import create_app
from assetstudio_server.operator_auth import ROLE_RULES, match_rule, required_role
from assetstudio_server.studio import build_studio
from fastapi.testclient import TestClient

from tests.conftest import HEADERS, Api, make_settings

SECRET = "s3cret-proxy-value"
GROUPS = {"owner": "assetstudio-owners", "reviewer": "assetstudio-reviewers", "viewer": "assetstudio-viewers"}
BAD_REGISTER: dict[str, Any] = {
    "schema": "assetstudio.runner.register.v1", "registration_token": "x" * 20, "name": "r1",
    "public_key": keys.public_key_b64(keys.generate_private_key()),
    "platform": {"os": "linux", "arch": "x86_64", "hostname": "h"}}


def _proxy_client(tmp_path: Path, **overrides: Any) -> Iterator[TestClient]:
    settings = make_settings(tmp_path, coordinator=False)
    settings.auth_mode, settings.proxy_secret = "proxy", SECRET
    for k, v in overrides.items():
        setattr(settings, k, v)
    with TestClient(create_app(settings, build_studio(settings))) as c:
        yield c


@pytest.fixture
def proxy(tmp_path: Path) -> Iterator[TestClient]:
    yield from _proxy_client(tmp_path)


def hdr(role: str | None = "owner", user: str | None = "alice", secret: str | None = SECRET,
        csrf: bool = True, **extra: str) -> dict[str, str]:
    h = dict(HEADERS) if csrf else {}
    if secret is not None:
        h["X-AssetStudio-Proxy-Secret"] = secret
    if user is not None:
        h["Remote-User"] = user
    if role is not None:
        h["Remote-Groups"] = f"other,{GROUPS[role]}"
    return {**h, **extra}


def test_every_mutating_route_has_an_explicit_rule(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, coordinator=False))
    missing = []
    for r in app.routes:
        methods = getattr(r, "methods", None) or set()
        path = getattr(r, "path", "")
        if not path.startswith("/api/") or path.startswith("/api/runner/"):
            continue
        concrete = re.sub(r"\{[^}]+\}", "x1", path)
        missing += [f"{m} {path}" for m in methods - {"GET", "HEAD", "OPTIONS"} if match_rule(m, concrete) is None]
    assert not missing, f"classify these routes in ROLE_RULES: {missing}"
    assert ROLE_RULES


def test_role_table_samples() -> None:
    p = "/api/v2/projects/p1"
    assert required_role("GET", f"{p}/runs") == "viewer"
    assert required_role("GET", "/api/v1/audit") == "owner"
    assert required_role("POST", f"{p}/runs/r1:approve-candidates") == "reviewer"
    assert required_role("POST", f"{p}/runs/r1:pause") == "reviewer"
    assert required_role("POST", f"{p}/jobs/j1:publish") == "reviewer"
    assert required_role("POST", f"{p}/jobs/j1:run") == "owner"
    assert required_role("POST", "/api/v1/runner-groups") == "owner"
    assert required_role("POST", "/api/v1/brand-new-route") == "owner"  # unmapped mutating: fail closed


def test_local_mode_needs_no_identity_and_acts_as_owner(api: Api) -> None:
    assert api.c.get("/api/v1/projects").status_code == 200
    gid = api.post("/api/v1/runner-groups", {"name": "g", "projects": "*", "operations": "*"})["id"]
    rows = api.get("/api/v1/audit")["rows"]
    assert any(r["event"] == "group_create" and r["actor"] == "operator" for r in rows)
    assert api.get(f"/api/v1/audit?runner_id={gid}")["rows"] == []


def test_proxy_requires_secret_and_identity(proxy: TestClient) -> None:
    assert proxy.get("/api/v1/projects").status_code == 401
    assert proxy.get("/api/v1/projects", headers=hdr(secret=None)).status_code == 401
    assert proxy.get("/api/v1/projects", headers=hdr(secret="wrong")).status_code == 401
    assert proxy.get("/api/v1/projects", headers=hdr(user=None)).status_code == 401
    r = proxy.get("/api/v1/projects", headers=hdr(role=None))
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert proxy.get("/api/v1/projects", headers=hdr("viewer")).status_code == 200
    assert proxy.get("/api/health").status_code == 200


def test_proxy_roles_gate_mutations(proxy: TestClient) -> None:
    group = {"name": "g", "projects": "*", "operations": "*"}
    assert proxy.post("/api/v1/projects", json={"name": "x"}, headers=hdr("viewer")).status_code == 403
    approve = "/api/v2/projects/nope/runs/r1:approve-candidates"
    assert proxy.post(approve, json={}, headers=hdr("viewer")).status_code == 403
    assert proxy.post(approve, json={}, headers=hdr("reviewer")).status_code not in (401, 403)
    assert proxy.post("/api/v1/runner-groups", json=group, headers=hdr("reviewer")).status_code == 403
    assert proxy.post("/api/v1/runner-groups", json=group, headers=hdr("owner", user="olga")).status_code == 200
    rows = proxy.get("/api/v1/audit", headers=hdr("owner"))
    assert any(r["event"] == "group_create" and r["actor"] == "olga" for r in rows.json()["rows"])


def test_csrf_still_enforced_in_proxy_mode(proxy: TestClient) -> None:
    r = proxy.post("/api/v1/projects", json={"name": "x"}, headers=hdr("owner", csrf=False))
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf"


def test_audit_view_is_owner_only(proxy: TestClient) -> None:
    assert proxy.get("/api/v1/audit", headers=hdr("reviewer")).status_code == 403
    assert proxy.get("/api/v1/audit", headers=hdr("viewer")).status_code == 403
    assert proxy.get("/api/v1/audit?limit=5", headers=hdr("owner")).status_code == 200


def test_runner_routes_ignore_operator_auth_but_are_rate_limited(proxy: TestClient) -> None:
    codes = [proxy.post("/api/runner/v1/register", json={}).status_code for _ in range(6)]
    assert codes[:5] == [400] * 5 and codes[5] == 429
    r = proxy.post("/api/runner/v1/register", json={})
    assert r.headers["retry-after"].isdigit()
    assert r.json()["code"] == "resource_exhausted"
    # Buckets are per endpoint: another unauthenticated endpoint still answers.
    assert proxy.post("/api/runner/v1/token/challenge", json={}).status_code == 400


def test_forwarded_for_ignored_without_proxy_secret(proxy: TestClient) -> None:
    codes = [proxy.post("/api/runner/v1/register", json={}, headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code
             for i in range(6)]
    assert codes[5] == 429


def test_forwarded_for_trusted_with_proxy_secret(proxy: TestClient) -> None:
    """The proxy appends the address it saw: the LAST hop counts; a client-forged first hop does not."""
    codes = [proxy.post("/api/runner/v1/register", json={}, headers={
        "X-AssetStudio-Proxy-Secret": SECRET, "X-Forwarded-For": f"6.6.6.6, 10.0.0.{i}"}).status_code
        for i in range(8)]
    assert 429 not in codes  # eight distinct real clients, one forged prefix
    forged = [proxy.post("/api/runner/v1/register", json={}, headers={
        "X-AssetStudio-Proxy-Secret": SECRET, "X-Forwarded-For": f"10.9.9.{i}, 10.0.0.200"}).status_code
        for i in range(6)]
    assert forged[5] == 429  # rotating a forged first hop does not escape the real client's bucket


def test_refused_registrations_aggregate_per_ip(tmp_path: Path) -> None:
    for c in _proxy_client(tmp_path, ratelimit_burst=50):
        for _ in range(4):
            assert c.post("/api/runner/v1/register", json=BAD_REGISTER).status_code == 403
        rows = [r for r in c.get("/api/v1/audit", headers=hdr("owner")).json()["rows"]
                if r["event"] == "register_refused"]
        assert len(rows) == 1 and rows[0]["detail"]["count"] == 4 and rows[0]["detail"]["ip"]


def test_proxy_mode_without_secret_refuses_startup(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, coordinator=False)
    settings.auth_mode = "proxy"
    with pytest.raises(ValueError, match="STUDIO_PROXY_SECRET"):
        create_app(settings, build_studio(settings))


def test_role_groups_env_parse() -> None:
    from assetstudio_server.settings import parse_role_groups
    assert parse_role_groups("owner:a, viewer:b") == {"owner": "a", "viewer": "b"}
    with pytest.raises(ValueError):
        parse_role_groups("admin:a")

