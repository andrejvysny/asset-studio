#!/usr/bin/env python3
"""Release export of a Godot project that consumes AssetStudio assets (AS-10, spec 00 §7).

Runs, in order, and stops at the first failure (nothing is exported unless every step passed):
  1. restore --locked [--offline]        materialize the exact locked deliveries (never rewrites the lock)
  2. headless import                      Godot imports every managed file
  3. export-preflight [--preset NAME]     locks, receipts, imports, presets and scene references validate
  4. godot --headless --export-release <preset> <out>

Editor export hooks (the addon dock) only add diagnostics; this wrapper is the gate. The exported game never talks
to AssetStudio: preflight fails when an exported scene references the addon's editor or network scripts.

Usage: python3 scripts/godot_export_wrapper.py --project DIR --preset NAME --out FILE [--godot PATH] [--offline]
                                               [--preflight-only]
Exit codes: 0 exported (or preflight passed with --preflight-only), 1 a step failed, 2 usage error.
Credentials: restore uses the connection registered with `connect` (user data dir, never the project). A token is
never read from argv or printed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

CLI = "res://addons/assetstudio/cli.gd"
STEP_TIMEOUT = 1800


def godot_cmd(godot: str, project: Path, *args: str) -> list[str]:
    return [godot, "--headless", "--path", str(project), *args]


def run_step(label: str, cmd: list[str]) -> subprocess.CompletedProcess[str]:
    print(f"[{label}] {' '.join(cmd[:1])} ...", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=STEP_TIMEOUT)
    if proc.stderr.strip():
        print(proc.stderr.rstrip(), file=sys.stderr)
    return proc


def parse_report(stdout: str) -> dict | None:
    for line in reversed(stdout.strip().splitlines()):
        if line.startswith("{"):
            try:
                report = json.loads(line)
            except json.JSONDecodeError:
                return None
            return report if report.get("command") == "export-preflight" else None
    return None


def refuse(why: str) -> int:
    print(f"refusing to export: {why}", file=sys.stderr)
    return 1


def preflight(godot: str, project: Path, preset: str, offline: bool) -> tuple[bool, dict | None]:
    args = ["--script", CLI, "--", "export-preflight", "--preset", preset] + (["--offline"] if offline else [])
    proc = run_step("export-preflight", godot_cmd(godot, project, *args))
    report = parse_report(proc.stdout)
    if report is not None:
        print(json.dumps(report, sort_keys=True))
    return proc.returncode == 0 and bool(report and report["ok"]), report


def export(godot: str, project: Path, preset: str, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.is_dir():  # macOS .app bundles are directories
        shutil.rmtree(out)
    else:
        out.unlink(missing_ok=True)
    proc = run_step("export", godot_cmd(godot, project, "--export-release", preset, str(out)))
    if proc.returncode != 0 or not out.exists():
        state = "present" if out.exists() else "missing"
        return refuse(f"godot export failed (exit {proc.returncode}, output {state})")
    print(f"exported {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--preset", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--godot", default=os.environ.get("GODOT_BIN") or shutil.which("godot") or "godot")
    ap.add_argument("--offline", action="store_true", help="restore from the local blob cache only")
    ap.add_argument("--preflight-only", action="store_true", help="stop after a passing export-preflight")
    args = ap.parse_args()
    project = args.project.resolve()
    if not (project / "project.godot").is_file():
        print("error: --project is not a Godot project", file=sys.stderr)
        return 2
    restore = ["--script", CLI, "--", "restore", "--locked"] + (["--offline"] if args.offline else [])
    if run_step("restore", godot_cmd(args.godot, project, *restore)).returncode != 0:
        return refuse("restore --locked failed")
    if run_step("import", godot_cmd(args.godot, project, "--editor", "--import")).returncode != 0:
        return refuse("headless import failed")
    ok, _ = preflight(args.godot, project, args.preset, args.offline)
    if not ok:
        return refuse("export-preflight reported problems")
    return 0 if args.preflight_only else export(args.godot, project, args.preset, args.out.resolve())


if __name__ == "__main__":
    sys.exit(main())
