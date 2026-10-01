"""Integration listener: open health, bearer + library scoping, error shape, body limits, product isolation."""
from __future__ import annotations

from assetstudio_core.ids import new_id
from assetstudio_server.integration_api.auth import principal
from assetstudio_server.integration_api.principal import Principal, require
from assetstudio_server.mcp_api.auth import TokenStore
from fastapi import Depends, FastAPI, Request

from tests.conftest import Api, new_project
from tests.integration_support import client, integration_app_for, make_token

V1 = "/api/integration/v1"
ERROR_KEYS = {"code", "message", "retryable", "details"}


def test_health_is_open_and_minimal(api: Api) -> None:
    app, _ = integration_app_for(api)
    r = client(app).get(f"{V1}/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "assetstudio-integration", "api_version": 1}


def test_capabilities_needs_a_live_token_and_server_id_is_stable(api: Api) -> None:
    app, fastapi_app = integration_app_for(api)
    lib = new_project(api)
    assert client(app).get(f"{V1}/capabilities").status_code == 401
    token = make_token(app, "godot", ["assets:read"], [lib])
    r = client(app, token).get(f"{V1}/capabilities")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["granted"] == {"library_ids": [lib], "scopes": ["assets:read"]}
    assert {"server_id", "api_version", "contract_version", "source_package_version", "representations",
            "known_capabilities", "limits"} <= body.keys()
    second, _ = integration_app_for(api)  # same instance dir, fresh build
    assert client(second, token).get(f"{V1}/capabilities").json()["server_id"] == body["server_id"]
    fastapi_app.state.tokens.revoke("godot")
    revoked = client(app, token).get(f"{V1}/capabilities")
    assert revoked.status_code == 401 and revoked.headers["www-authenticate"] == "Bearer"


def test_unauthorized_error_body_shape(api: Api) -> None:
    app, _ = integration_app_for(api)
    r = client(app, "asi_wrong").get(f"{V1}/capabilities")
    assert r.status_code == 401
    assert r.json() == {"error": {"code": "unauthorized", "message": "valid bearer token required",
                                  "retryable": False, "details": {}}}


def test_libraries_lists_only_granted_and_isolates_failures(api: Api) -> None:
    app, _ = integration_app_for(api)
    real, other, ghost = new_project(api, "Real"), new_project(api, "Other"), new_id("prj")
    token = make_token(app, "godot", ["assets:read"], [ghost, real])
    libs = client(app, token).get(f"{V1}/libraries").json()["libraries"]
    by_id = {x["library_id"]: x for x in libs}
    assert set(by_id) == {real, ghost} and other not in by_id
    assert by_id[real] == {"library_id": real, "name": "Real", "scopes": ["assets:read"], "state": "available"}
    assert by_id[ghost] == {"library_id": ghost, "name": None, "scopes": ["assets:read"], "state": "unavailable"}
    assert [x["library_id"] for x in libs] == sorted(by_id)


def test_ungranted_and_nonexistent_library_are_indistinguishable(api: Api) -> None:
    app, fastapi_app = integration_app_for(api)
    granted, ungranted, missing = new_project(api, "A"), new_project(api, "B"), new_id("prj")
    probe = FastAPI()

    @probe.get("/probe/{library_id}")
    def _probe(library_id: str, who: Principal = Depends(principal)) -> dict[str, bool]:
        require(who, "assets:read", library_id)
        return {"ok": True}

    fastapi_app.include_router(probe.router, prefix=V1)
    c = client(app, make_token(app, "godot", ["assets:read"], [granted]))
    assert c.get(f"{V1}/probe/{granted}").status_code == 200
    a, b = c.get(f"{V1}/probe/{ungranted}"), c.get(f"{V1}/probe/{missing}")
    assert a.status_code == b.status_code == 403
    assert a.json() == b.json() and a.json()["error"]["code"] == "forbidden"
    assert set(a.json()["error"]) == ERROR_KEYS
    pub = client(app, make_token(app, "pub", ["assets:publish"], [granted]))
    assert pub.get(f"{V1}/probe/{granted}").status_code == 403  # publish does not imply read


def test_product_routes_are_not_served(api: Api) -> None:
    app, _ = integration_app_for(api)
    c = client(app, make_token(app, "godot", ["assets:read"], [new_project(api)]))
    for path in ("/api/v1/projects", "/", "/api/health", "/docs"):
        r = c.get(path)
        assert r.status_code == 404, path
        assert r.json()["error"]["code"] == "not_found" and set(r.json()["error"]) == ERROR_KEYS


def test_mcp_token_is_rejected(api: Api) -> None:
    app, _ = integration_app_for(api)
    mcp_token = TokenStore(api.studio.settings.instance_dir / "mcp_tokens.json").create("agent", "full")
    assert client(app, mcp_token).get(f"{V1}/capabilities").status_code == 401


def test_body_limit_and_auth_ordering(api: Api) -> None:
    app, _ = integration_app_for(api)
    big = b"x" * 70000
    token = make_token(app, "godot", ["assets:read"], [new_project(api)])
    r = client(app, token).post(f"{V1}/anything", content=big)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "resource_limit" and set(r.json()["error"]) == ERROR_KEYS
    assert client(app).post(f"{V1}/anything", content=big).status_code == 401  # auth before the body is considered
    small = client(app, token).post(f"{V1}/anything", content=b"x" * 100)
    assert small.status_code == 404


def test_streamed_body_over_limit_is_cut_off(api: Api) -> None:
    app, fastapi_app = integration_app_for(api)

    @fastapi_app.post(f"{V1}/echo")
    async def _echo(request: Request) -> dict[str, int]:
        return {"n": len(await request.body())}

    token = make_token(app, "godot", ["assets:read"], [new_project(api)])
    chunks = (b"y" * 30000 for _ in range(4))  # chunked: no Content-Length
    r = client(app, token).post(f"{V1}/echo", content=chunks)
    assert r.status_code == 413 and r.json()["error"]["code"] == "resource_limit"
