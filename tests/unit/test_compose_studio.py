"""compose.studio.yml (Studio only) + compose.public.yml (every listener through Traefik), Compose >= 2.24.4."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPOOFABLE = ("Remote-User", "Remote-Groups", "X-AssetStudio-Proxy-Secret", "X-AssetStudio-Internal",
             "X-AssetStudio-Actor", "X-AssetStudio-Agent-Scope")
MCP_INTEGRATION = ("assetstudio-mcp", "assetstudio-integration")


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / name).read_text())


def _text(name: str) -> str:
    return (ROOT / name).read_text()


def _labels() -> dict[str, str]:
    return _load("compose.public.yml")["services"]["studio"]["labels"]


def _mws(router: str) -> list[str]:
    return _labels()[f"traefik.http.routers.{router}.middlewares"].split(",")


def test_studio_only_has_no_gpu_engine_runner_or_models() -> None:
    doc = _load("compose.studio.yml")
    assert list(doc["services"]) == ["studio"]
    studio = doc["services"]["studio"]
    assert "ports" not in studio and "deploy" not in studio and "devices" not in studio
    assert studio["environment"]["STUDIO_EXECUTION"] == "nodes"
    assert studio["networks"] == ["internal"] and doc["networks"]["internal"]["internal"] is True
    assert not any("/models" in v for v in studio["volumes"])
    assert any(v.endswith(":/data/instance") for v in studio["volumes"])
    for key in ("COMFY_URL", "AUX_URL", "WORKER3D_URL"):
        assert studio["environment"][key] == ""
    text = _text("compose.studio.yml").lower()
    assert "nvidia" not in text and "comfyui:" not in text and "runner:" not in text.replace("runners", "")


def test_public_overlay_routes_all_three_listeners() -> None:
    labels = _labels()
    port = "traefik.http.services.{}.loadbalancer.server.port"
    assert labels[port.format("assetstudio")] == "8190"
    assert labels[port.format("assetstudio-mcp")] == "8191"
    assert labels[port.format("assetstudio-integration")] == "8192"
    for router, svc in (("assetstudio-mcp", "assetstudio-mcp"), ("assetstudio-integration", "assetstudio-integration")):
        assert labels[f"traefik.http.routers.{router}.service"] == svc
        assert "tls.certresolver" in "".join(k for k in labels if k.startswith(f"traefik.http.routers.{router}."))
    assert "/api/integration/v1" in labels["traefik.http.routers.assetstudio-integration.rule"]
    assert "mcp." in labels["traefik.http.routers.assetstudio-mcp.rule"]
    env = _load("compose.public.yml")["services"]["studio"]["environment"]
    assert env["STUDIO_MCP"].startswith("${STUDIO_MCP") and env["STUDIO_INTEGRATION_ENABLED"].startswith("${STUDIO_INTEGRATION")
    assert env["STUDIO_MCP_PUBLIC_URL"].startswith("https://")


def test_mcp_and_integration_skip_authelia_and_secret_but_strip_every_identity_header() -> None:
    labels = _labels()
    prefix = "traefik.http.middlewares.assetstudio-strip.headers.customrequestheaders."
    for h in SPOOFABLE:
        assert labels[prefix + h] == "", h  # blank = stripped, never stamped
    for router in MCP_INTEGRATION:
        mws = _mws(router)
        assert mws[0] == "assetstudio-strip"
        assert not any("authelia" in m.lower() for m in mws)
        assert "assetstudio-headers" not in mws  # that one stamps the operator proxy secret
        assert any(m.endswith("-rl") for m in mws) and any(m.endswith("-body") for m in mws)
    assert labels["traefik.http.middlewares.assetstudio-integration-body.buffering.maxrequestbodybytes"] == str(512 * 1024 * 1024)
    mcp_cap = int(labels["traefik.http.middlewares.assetstudio-mcp-body.buffering.maxrequestbodybytes"])
    assert 0 < mcp_cap < 512 * 1024 * 1024


def test_operator_router_still_behind_authelia() -> None:
    mws = _mws("assetstudio")
    assert mws[0] == "assetstudio-headers" and any(m.startswith("${AUTHELIA_MIDDLEWARE") for m in mws)


def test_compose_minimum_is_2_24_4_everywhere() -> None:
    for name in ("compose.studio.yml", "compose.public.yml", "compose.nodes.yml", "compose.node-remote.yml"):
        assert "2.24.4" in _text(name), name
    assert "2.24.4" in _text("Makefile")
    assert "2.24.4" in (ROOT / "docs" / "installation.md").read_text()


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_docker_compose_renders_studio_plus_public() -> None:
    cmd = ["docker", "compose", "-f", "compose.studio.yml", "-f", "compose.public.yml", "config", "--format", "json"]
    env = {**os.environ, "STUDIO_HOST": "studio.example.com", "STUDIO_PROXY_SECRET": "x"}
    try:
        out = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        pytest.skip(f"docker compose unavailable: {e}")
    if out.returncode != 0 and "not a docker command" in out.stderr:
        pytest.skip("docker compose plugin unavailable")
    assert out.returncode == 0, out.stderr
    cfg = json.loads(out.stdout)
    assert list(cfg["services"]) == ["studio"]
    studio = cfg["services"]["studio"]
    assert "ports" not in studio and "devices" not in json.dumps(studio)
    assert set(studio["networks"]) == {"internal", "proxy"}
    labels = studio["labels"]
    assert labels["traefik.http.routers.assetstudio-mcp.rule"].startswith("Host(`mcp.studio.example.com`)")
    assert "integration.studio.example.com" in labels["traefik.http.routers.assetstudio-integration.rule"]
