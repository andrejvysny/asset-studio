"""Integration-listener test harness: the real stack (auth -> body limit -> FastAPI), in-process."""
from __future__ import annotations

from assetstudio_server.integration_api.app import IntegrationApp, build_integration_app
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import ASGIApp

from tests.conftest import Api

BASE_URL = "http://127.0.0.1:8192"


def integration_app_for(api: Api) -> tuple[IntegrationApp, FastAPI]:
    app = build_integration_app(api.studio, api.studio.settings)
    return app, app.fastapi


def client(asgi_app: ASGIApp, token: str | None = None) -> TestClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(asgi_app, base_url=BASE_URL, headers=headers, raise_server_exceptions=False)


def make_token(app: IntegrationApp | FastAPI, name: str, scopes: list[str], libraries: list[str]) -> str:
    fastapi_app = app.fastapi if isinstance(app, IntegrationApp) else app
    return fastapi_app.state.tokens.create(name, scopes, libraries)
