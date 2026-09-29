"""Browser tests against a real Studio process with the SIMULATED engine (labelled in the UI). Not GPU evidence."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "tests" / "e2e" / "artifacts"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def studio_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if not (ROOT / "web" / "dist" / "index.html").is_file():
        pytest.skip("web/dist missing: run `make web-build`")
    tmp = tmp_path_factory.mktemp("studio")
    port = _free_port()
    env = {**os.environ, "STUDIO_ENGINE": "fake", "STUDIO_INSTANCE_DIR": str(tmp / "instance"),
           "STUDIO_PROJECT_ROOTS": str(tmp / "projects"), "STUDIO_WEB_DIR": str(ROOT / "web" / "dist"),
           "STUDIO_MODELS_ROOT": str(ROOT / "models"), "STUDIO_PORT": str(port)}
    (tmp / "projects").mkdir()
    proc = subprocess.Popen([sys.executable, "-m", "assetstudio_server.cli", "serve"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{url}/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError(proc.stdout.read().decode() if proc.stdout else "studio did not start")
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
def page(studio_url: str):
    from playwright.sync_api import sync_playwright

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1440, "height": 900}, base_url=studio_url)
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.errors = errors  # type: ignore[attr-defined]
        external: list[str] = []
        pg.on("request", lambda r: external.append(r.url) if not r.url.startswith(("http://127.0.0.1", "data:", "blob:"))
              else None)
        pg.external = external  # type: ignore[attr-defined]
        yield pg
        browser.close()


def shot(page, name: str) -> None:
    page.screenshot(path=str(ARTIFACTS / f"{name}.png"), full_page=True)
