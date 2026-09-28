"""Playwright e2e for Asset Studio. Run: make e2e (needs web/dist built; live tests need the stack up)."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, ConsoleMessage, Page, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = Path(os.environ.get("E2E_ARTIFACTS", ROOT / "tests" / "e2e" / "artifacts"))
sys.path.insert(0, str(ROOT / "comfyui" / "custom_nodes" / "line_a"))
sys.path.insert(0, str(ROOT / "services" / "trellis_worker"))


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


class StudioPage:
    """Page wrapper: base URL, screenshots, and collected browser errors."""

    def __init__(self, page: Page, base: str, name: str) -> None:
        self.page, self.base, self.name = page, base, name
        self.errors: list[str] = []
        page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        page.on("console", self._console)

    def _console(self, m: ConsoleMessage) -> None:
        if m.type == "error":
            self.errors.append(f"console: {m.text}")

    def goto(self, path: str, settle_ms: int = 1500) -> Page:
        self.page.goto(self.base + path)
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(settle_ms)
        return self.page

    def shot(self, label: str) -> None:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(ARTIFACTS / f"{self.name}__{label}.png"))


def _studio_page(browser: Browser, base: str, request: pytest.FixtureRequest) -> Iterator[StudioPage]:
    ctx = browser.new_context(viewport={"width": 1500, "height": 1000})
    sp = StudioPage(ctx.new_page(), base, request.node.name)
    yield sp
    ctx.close()
    assert not sp.errors, "browser errors:\n" + "\n".join(sp.errors)


# ---------------------------------------------------------------- isolated fixture server
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_fixture_srv(srv: Path) -> tuple[str, str]:
    """Temp data root with one completed, validated attempt (real textured GLB). Returns (job_id, attempt_id)."""
    import meshcheck
    import numpy as np
    import trimesh
    from jobcore.job_io import Job
    from PIL import Image

    shutil.copytree(ROOT / "config", srv / "config")
    (srv / "output").mkdir()
    (srv / "library").mkdir()
    (srv / "models" / "manifests").mkdir(parents=True)
    job = Job.create(srv / "output", {"prompt": "e2e fixture barrel"})
    job.write_json("enhancement.json", {"short_title": "E2E barrel", "description": "fixture"})
    a = job.new_attempt({"index": 1, "candidate": "01.png", "candidate_set": "cs-1", "qa_status": "recommended"})
    adir = job.path(job.attempt_dir(a["id"]))
    tex = Image.new("RGB", (256, 256), (150, 95, 50))
    tex.save(adir / "selected.png")
    cyl = trimesh.creation.cylinder(radius=0.4, height=1.0, sections=32)
    uv = np.c_[(np.arctan2(cyl.vertices[:, 1], cyl.vertices[:, 0]) / (2 * np.pi)) % 1, cyl.vertices[:, 2] + 0.5]
    cyl.visual = trimesh.visual.TextureVisuals(uv=uv, material=trimesh.visual.material.PBRMaterial(baseColorTexture=tex))
    (adir / "processed").mkdir(parents=True)
    glb = adir / "processed" / "model.glb"
    cyl.export(glb)
    val = meshcheck.validate_glb(glb)
    assert val["ok"], val
    job.update_attempt(a["id"], state="completed", validated=True, validation=val, mesh=meshcheck.mesh_info(cyl, glb, 0),
                       triangles={"requested": 500, "decimation_target": 2000, "reason": "triangle_floor",
                                  "actual": len(cyl.faces)}, outputs={"glb": "processed/model.glb"})
    state = job.read_json("job_state.json")
    state.update(state="completed", current_attempt=a["id"])
    job.write_json("job_state.json", state)
    return job.id, a["id"]


@pytest.fixture(scope="module")
def fixture_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict]:
    web = ROOT / "web" / "dist" / "index.html"
    if not web.is_file():
        pytest.skip("web/dist not built (make web-build)")
    srv = tmp_path_factory.mktemp("srv")
    job_id, attempt_id = make_fixture_srv(srv)
    port = _free_port()
    env = {**os.environ, "SRV_ROOT": str(srv), "WEB_DIR": str(ROOT / "web" / "dist"),
           "COMFY_URL": "http://127.0.0.1:9",  # unreachable on purpose: library must degrade gracefully
           "PROMPT_SERVICE_URL": "http://127.0.0.1:9", "TRELLIS_WORKER_URL": "http://127.0.0.1:9",
           "PYTHONPATH": f"{ROOT / 'services' / 'library'}:{ROOT / 'comfyui' / 'custom_nodes' / 'line_a'}"}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/catalog", timeout=1)
            break
        except OSError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("fixture library server did not start")
    yield {"base": base, "job_id": job_id, "attempt_id": attempt_id, "srv": srv}
    proc.terminate()
    proc.wait(10)


@pytest.fixture
def fx_page(browser: Browser, fixture_server: dict, request: pytest.FixtureRequest) -> Iterator[StudioPage]:
    yield from _studio_page(browser, fixture_server["base"], request)


# ---------------------------------------------------------------- live stack
LIVE = os.environ.get("STUDIO_URL", "http://127.0.0.1:8190")


@pytest.fixture(scope="session")
def live_base() -> str:
    try:
        urllib.request.urlopen(LIVE + "/api/catalog", timeout=3)
    except OSError:
        pytest.skip(f"live Studio not reachable at {LIVE}")
    return LIVE


@pytest.fixture
def live_page(browser: Browser, live_base: str, request: pytest.FixtureRequest) -> Iterator[StudioPage]:
    yield from _studio_page(browser, live_base, request)
