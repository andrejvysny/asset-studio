#!/usr/bin/env python3
"""Plugin tests for the AssetStudio addon (AS-08), in a temp consumer project.

1. Smoke: the plugin loads in a headless editor with no connection configured (no SCRIPT ERROR / parse error).
2. Smoke with a configured connection to fake_server.py: the dock lists libraries/assets without errors.
3. Editor self-test (tests/editor_selftest, test-only plugin): install -> import -> finalize -> place with
   undo/redo -> update badge -> review -> update selected instances -> update binding -> restore previous, all
   through the dock's real action code inside a headless editor.

Drag and drop and the visual dock are NOT covered here: see addons/assetstudio/README.md (manual checks).
Usage: python3 integrations/godot/tests/run_plugin_tests.py [--godot PATH] [--keep]
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from run_consumer_tests import (CLI, CONTRACTS, SERVER_ID, HERE, PROJECT, start_server, user_dirs)  # noqa: F401

TIMEOUT = 240
BAD = ("SCRIPT ERROR", "Parse Error", "Failed to load script", "Compile Error", "Failed to instantiate")
failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def make_project(root: Path, user_dir_name: str, plugins: list[str]) -> None:
    root.mkdir(parents=True)
    enabled = ", ".join(f'"{p}"' for p in plugins)
    (root / "project.godot").write_text(
        "config_version=5\n\n[application]\n\nconfig/name=\"AssetStudio Plugin E2E\"\n"
        "config/features=PackedStringArray(\"4.7\")\nconfig/use_custom_user_dir=true\n"
        f"config/custom_user_dir_name=\"{user_dir_name}\"\n\n[editor_plugins]\n\n"
        f"enabled=PackedStringArray({enabled})\n")
    shutil.copytree(PROJECT / "addons" / "assetstudio", root / "addons" / "assetstudio")
    (root / "level.tscn").write_text(
        '[gd_scene format=3]\n\n[node name="Level" type="Node3D"]\n')


def run(godot: str, project: Path, args: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    proc = subprocess.run([godot, "--headless", "--path", str(project), *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=TIMEOUT, env=dict(os.environ, **(env or {})))
    return proc.returncode, strip_ansi(proc.stdout + proc.stderr)


def clean(text: str) -> bool:
    return not any(b in text for b in BAD)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--godot", default=shutil.which("godot") or "godot")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    token = "tok_" + secrets.token_hex(16)
    user_name = "assetstudio_plugin_" + secrets.token_hex(4)
    tmp = Path(tempfile.mkdtemp(prefix="as_plugin_"))
    server = None
    try:
        proj = tmp / "smoke"
        make_project(proj, user_name, ["res://addons/assetstudio/plugin.cfg"])
        code, out = run(args.godot, proj, ["--editor", "--quit-after", "120"])
        check(code == 0 and clean(out), f"plugin loads without a connection ({out[-300:] if not clean(out) else ''})")
        server, port = start_server(token)
        token_file = tmp / "token.txt"
        token_file.write_text(token + "\n")
        token_file.chmod(0o600)
        run(args.godot, proj, ["--script", CLI, "--", "connect", "--server-id", SERVER_ID, "--url",
                               f"http://127.0.0.1:{port}", "--token-file", str(token_file)])
        code, out = run(args.godot, proj, ["--editor", "--quit-after", "240"])
        check(code == 0 and clean(out), f"plugin loads with a configured connection ({out[-300:] if not clean(out) else ''})")
        run_selftest(args.godot, tmp, user_name, token_file, port)
    finally:
        if server is not None:
            server.terminate()
            server.wait(timeout=10)
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
            for d in user_dirs(user_name):
                shutil.rmtree(d, ignore_errors=True)
    print(f"\n{'FAILED' if failures else 'ALL PASSED'}: {len(failures)} failure(s)")
    return 1 if failures else 0


def run_selftest(godot: str, tmp: Path, user_name: str, token_file: Path, port: str) -> None:
    proj = tmp / "selftest"
    make_project(proj, user_name, ["res://addons/assetstudio/plugin.cfg", "res://addons/as_selftest/plugin.cfg"])
    shutil.copytree(HERE / "editor_selftest", proj / "addons" / "as_selftest")
    p = subprocess.run([godot, "--headless", "--path", str(proj), "--script", CLI, "--", "connect", "--server-id",
                        SERVER_ID, "--url", f"http://127.0.0.1:{port}", "--token-file", str(token_file)],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=TIMEOUT)
    check(p.returncode == 0, "selftest project: connect")
    code, out = run(godot, proj, ["--editor"], {"ASSETSTUDIO_SELFTEST": "1"})
    for line in out.splitlines():
        if line.startswith("STEP "):
            check(" PASS" in line, line)
    check("SELFTEST_OK" in out and code == 0, f"editor self-test finished OK (exit {code})")
    check(clean(out), f"editor self-test output has no script errors ({[l for l in out.splitlines() if any(b in l for b in BAD)][:3]})")


if __name__ == "__main__":
    sys.exit(main())
