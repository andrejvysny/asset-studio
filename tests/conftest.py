"""Shared fixtures. Engines here are SIMULATED (FakeEngine/FakeAux): contract evidence only, never GPU proof."""
from __future__ import annotations

import io
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from assetstudio_server.adapters.fake import FakeAux, FakeEngine
from assetstudio_server.main import create_app
from assetstudio_server.settings import Settings
from assetstudio_server.studio import Studio, build_studio
from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
HEADERS = {"x-assetstudio": "1"}


def make_settings(tmp: Path, engine: str = "fake", coordinator: bool = True) -> Settings:
    s = Settings()
    s.instance_dir = tmp / "instance"
    s.project_roots = [tmp / "projects"]
    s.config_dir = ROOT / "config"
    s.workflows_dir = ROOT / "comfyui" / "workflows"
    s.models_root = tmp / "no-models"
    s.web_dir = tmp / "no-web"
    s.engine = engine
    s.start_coordinator = coordinator
    s.instance_id = "test-instance"
    return s


class Api:
    def __init__(self, client: TestClient, studio: Studio) -> None:
        self.c, self.studio = client, studio

    def get(self, path: str, **kw: Any) -> Any:
        r = self.c.get(path, **kw)
        assert r.status_code == 200, r.text
        return r.json()

    def post(self, path: str, json: Any = None, status: int | tuple[int, ...] = (200, 201, 202), **kw: Any) -> Any:
        r = self.c.post(path, json=json, headers=HEADERS, **kw)
        ok = status if isinstance(status, tuple) else (status,)
        assert r.status_code in ok, f"{r.status_code} {r.text}"
        return r.json()

    def raw(self, method: str, path: str, **kw: Any) -> Any:
        return self.c.request(method, path, headers={**HEADERS, **kw.pop("headers", {})}, **kw)

    def wait_ops(self, timeout: float = 20.0) -> None:
        """Wait until no operation is queued/running (simulated engine finishes quickly)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            active = self.studio.journal.list(states=("held", "queued", "running", "reconciling", "cancel_requested"))
            if not active:
                return
            time.sleep(0.05)
        raise AssertionError(f"operations still active: {[(o.kind, o.state) for o in active]}")


@pytest.fixture
def make_api(tmp_path: Path) -> Iterator[Callable[..., Api]]:
    clients: list[TestClient] = []

    def factory(engine: str = "fake", coordinator: bool = True, instance: Path | None = None,
                studio: Studio | None = None) -> Api:
        settings = make_settings(instance or tmp_path, engine, coordinator)
        st = studio or build_studio(settings)
        client = TestClient(create_app(settings, st))
        client.__enter__()
        clients.append(client)
        return Api(client, st)

    yield factory
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def api(make_api: Callable[..., Api]) -> Api:
    return make_api()


@pytest.fixture
def fake_studio(tmp_path: Path) -> Studio:
    return build_studio(make_settings(tmp_path), engine=FakeEngine(), aux=FakeAux())


def png_bytes(w: int = 64, h: int = 64, color: tuple[int, int, int] = (200, 80, 40), alpha: bool = False) -> bytes:
    im = Image.new("RGBA" if alpha else "RGB", (w, h), (*color, 255) if alpha else color)
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def new_project(api: Api, name: str = "Demo") -> str:
    return api.post("/api/v1/projects", {"name": name})["id"]
