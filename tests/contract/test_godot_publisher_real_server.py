"""The Godot addon's `publish` command against the REAL integration listener (not fake_server.py).

Skipped without a `godot` binary. Publishes fixture-equivalent scenes headlessly: preview + commit as a new asset,
a new version with the expected base version, and a stale base version (stale_pointer, nothing published).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import pytest
import uvicorn

from tests.integration_publication_support import make_env

ROOT = Path(__file__).resolve().parents[2]
GODOT_DIR = ROOT / "integrations" / "godot"
FIXTURES = ROOT / "contracts" / "godot-integration" / "v1" / "fixtures" / "source_packages" / "valid"
CLI = "res://addons/assetstudio/cli.gd"
pytestmark = pytest.mark.skipif(shutil.which("godot") is None, reason="godot binary not installed")


def serve(app: object) -> tuple[uvicorn.Server, int]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started and server.servers and server.servers[0].sockets:
            return server, server.servers[0].sockets[0].getsockname()[1]
        time.sleep(0.05)
    raise RuntimeError("integration listener did not start")


def godot(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["godot", "--headless", "--path", str(project), *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=240)


def objects(text: str) -> list[dict]:
    out, dec, i = [], json.JSONDecoder(), text.find("{")
    while 0 <= i < len(text):
        obj, end = dec.raw_decode(text, i)
        out.append(obj)
        i = text.find("{", end)
    return out


def test_publish_commit_new_version_and_stale_pointer(make_api, tmp_path) -> None:
    env = make_env(make_api)
    server, port = serve(env.app)
    try:
        project = tmp_path / "project"
        project.mkdir()
        (project / "project.godot").write_text(
            'config_version=5\n\n[application]\n\nconfig/name="real"\nconfig/features=PackedStringArray("4.7")\n'
            f'config/use_custom_user_dir=true\nconfig/custom_user_dir_name="as_real_{os.getpid()}"\n')
        shutil.copytree(GODOT_DIR / "addons" / "assetstudio", project / "addons" / "assetstudio")
        for fixture in ("primitive_prop", "textured_tree"):
            with zipfile.ZipFile(FIXTURES / f"{fixture}.zip") as z:
                for name in z.namelist():
                    if name != "source_manifest.json":
                        z.extract(name, project)
        token_file = tmp_path / "token"
        token_file.write_text(env.token + "\n")
        godot(project, "--import")
        done = godot(project, "--script", CLI, "--", "connect", "--server-id", env.server_id, "--url",
                     f"http://127.0.0.1:{port}", "--token-file", str(token_file))
        assert done.returncode == 0, done.stderr
        first = godot(project, "--script", CLI, "--", "publish", "--library", env.lib, "--scene",
                      "res://scenes/prop.tscn", "--commit", "--name", "Real Prop")
        assert first.returncode == 0, first.stderr
        review, outcome = objects(first.stdout)[:2]
        assert review["phase"] == "previewed" and outcome["phase"] == "committed"
        asset_id, version_1 = outcome["outcome"]["asset_id"], outcome["outcome"]["version_id"]
        detail = env.c.get(env.url(f"/assets/{asset_id}")).json()
        assert detail["current_version_id"] == version_1
        second = godot(project, "--script", CLI, "--", "publish", "--library", env.lib, "--scene",
                       "res://scenes/tree.tscn", "--new-version-of", asset_id, "--expected-current", version_1,
                       "--commit")
        assert second.returncode == 0, second.stderr
        version_2 = objects(second.stdout)[1]["outcome"]["version_id"]
        assert version_2 != version_1
        assert env.c.get(env.url(f"/assets/{asset_id}")).json()["current_version_id"] == version_2
        stale = godot(project, "--script", CLI, "--", "publish", "--library", env.lib, "--scene",
                      "res://scenes/prop.tscn", "--new-version-of", asset_id, "--expected-current", version_1,
                      "--commit", "--name", "Stale")
        assert stale.returncode == 1 and "conflict" in stale.stderr and version_2 in stale.stderr
        assert env.c.get(env.url(f"/assets/{asset_id}")).json()["current_version_id"] == version_2
        assert env.token not in first.stdout + first.stderr
    finally:
        server.should_exit = True
