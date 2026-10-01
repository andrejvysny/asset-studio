#!/usr/bin/env python3
"""Starts the fake server, runs the Godot client tests against it, stops the server, propagates the exit code.

Usage: python3 integrations/godot/tests/run_client_tests.py [--godot PATH] [--filter SUBSTRING]
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
CONTRACTS = PROJECT.parents[1] / "contracts" / "godot-integration" / "v1"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--godot", default=shutil.which("godot") or "godot")
    ap.add_argument("--filter", default="client")
    args = ap.parse_args()
    token = "tok_" + secrets.token_hex(16)
    server = subprocess.Popen(
        [sys.executable, str(HERE / "fake_server.py"), "0", "--contracts-dir", str(CONTRACTS)],
        stdout=subprocess.PIPE, text=True, env=dict(os.environ, ASSETSTUDIO_FAKE_TOKEN=token))
    try:
        line = server.stdout.readline().strip() if server.stdout else ""
        if not line.startswith("PORT "):
            print("fake server failed to start", file=sys.stderr)
            return 2
        env = dict(os.environ, ASSETSTUDIO_FAKE_PORT=line.split()[1], ASSETSTUDIO_FAKE_TOKEN=token,
                   ASSETSTUDIO_CONTRACTS_DIR=str(CONTRACTS))
        cmd = [args.godot, "--headless", "--path", str(PROJECT), "--script", "res://tests/run_tests.gd",
               "--", f"--filter={args.filter}"]
        proc = subprocess.run(cmd, env=env, timeout=600, capture_output=True, text=True)
        out = proc.stdout + proc.stderr
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        if token in out:
            print("FAIL: bearer token leaked into Godot output", file=sys.stderr)
            return 1
        if "SCRIPT ERROR" in out:
            print("FAIL: GDScript runtime errors in output", file=sys.stderr)
            return 1
        return proc.returncode
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
