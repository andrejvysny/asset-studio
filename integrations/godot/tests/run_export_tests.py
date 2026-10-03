#!/usr/bin/env python3
"""Export preflight + export wrapper E2E (AS-10): consumer project from run_consumer_tests (fake server), then
export-preflight scenarios (missing/modified/not imported, presets, forbidden references) and
scripts/godot_export_wrapper.py end to end (online, offline, refusal on tampering). The real macOS export runs when
the export template is installed; otherwise it is reported as NOT RUN.

Usage: python3 integrations/godot/tests/run_export_tests.py [--godot PATH] [--keep]
"""
from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import run_consumer_tests as C

WRAPPER = C.PROJECT.parents[1] / "scripts" / "godot_export_wrapper.py"
GOOD_EXCLUDES = (".assetstudio/*,*.token,server.token,connections.json,credentials.json,.env,"
                 "addons/assetstudio/*,export_presets.cfg")
SECRET = "SECRET-" + secrets.token_hex(8)


def presets(exclude: str) -> str:
    return (f'[preset.0]\n\nname="macOS"\nplatform="macOS"\nrunnable=true\nadvanced_options=false\n'
            f'dedicated_server=false\ncustom_features=""\nexport_filter="all_resources"\ninclude_filter=""\n'
            f'exclude_filter="{exclude}"\nexport_path=""\n\n[preset.0.options]\n\n'
            'application/bundle_identifier="com.example.asexport"\napplication/short_version="1.0"\n'
            'application/version="1.0"\ncodesign/codesign=0\nnotarization/notarization=0\n'
            'binary_format/architecture="universal"\n')


def template_installed() -> bool:
    base = Path.home() / "Library" / "Application Support" / "Godot" / "export_templates"
    return any(base.glob("*/macos.zip"))


def preflight(g: C.Godot, *extra: str) -> tuple[int, dict]:
    p = g.cli("export-preflight", *extra)
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("{")), "{}")
    return p.returncode, json.loads(line)


def codes(report: dict) -> set[str]:
    return {x["code"] for x in report.get("problems", [])}


def wrapper(godot: str, proj: Path, out: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(WRAPPER), "--project", str(proj), "--preset", "macOS", "--out", str(out),
                           "--godot", godot, *extra], capture_output=True, text=True, timeout=1800)


def prepare(g: C.Godot, proj: Path, tmp: Path, token_file: Path, port: str) -> None:
    url = f"http://127.0.0.1:{port}"
    check = C.check
    connect = g.cli("connect", "--server-id", C.SERVER_ID, "--url", url, "--token-file", str(token_file))
    check(connect.returncode == 0, "connect")
    check(g.cli("add", "--library", C.LIBRARY, "--asset", C.ASSET, "--version", "ver_00000000000000v1",
                "--preserve").returncode == 0, "add v1")
    (proj / "main.tscn").write_text('[gd_scene format=3]\n\n[node name="Main" type="Node3D"]\n')
    with (proj / "project.godot").open("a") as f:
        f.write('\n[application]\n\nrun/main_scene="res://main.tscn"\n\n[rendering]\n\n'
                'textures/vram_compression/import_etc2_astc=true\n')
    (proj / "server.token").write_text(SECRET)
    (proj / "export_presets.cfg").unlink(missing_ok=True)
    check(g.run(["--editor", "--import"]).returncode == 0, "import")
    check(g.cli("finalize").returncode == 0, "finalize")


def preflight_scenarios(g: C.Godot, proj: Path) -> None:
    check = C.check
    rc, rep = preflight(g, "--preset", "macOS")
    check(rc == 1 and "export_presets_missing" in codes(rep), f"no export_presets.cfg is refused ({codes(rep)})")
    (proj / "export_presets.cfg").write_text(presets(""))
    rc, rep = preflight(g, "--preset", "macOS")
    check(rc == 1 and "preset_missing_exclude" in codes(rep), "a preset that does not exclude private files is refused")
    (proj / "export_presets.cfg").write_text(presets("assets/library/*," + GOOD_EXCLUDES))
    rc, rep = preflight(g, "--preset", "macOS")
    check(rc == 1 and "preset_excludes_managed" in codes(rep), "a preset that excludes the managed root is refused")
    (proj / "export_presets.cfg").write_text(presets(GOOD_EXCLUDES))
    rc, rep = preflight(g, "--preset", "macOS", "--offline")
    check(rc == 0 and rep["ok"] and rep["problems"] == [] and rep["checked"]["deliveries"] == 1
          and rep["offline"], f"clean project passes preflight ({rep})")
    rc, rep = preflight(g, "--preset", "nope")
    check(rc == 1 and "export_presets_missing" in codes(rep), "an unknown preset name is refused")
    victim = next((proj / "assets" / "library").rglob("portable.glb"))
    original = victim.read_bytes()
    victim.write_bytes(original + b"x")
    rc, rep = preflight(g, "--preset", "macOS")
    check(rc == 1 and "dependency_integrity" in codes(rep), "a modified managed file blocks export")
    victim.write_bytes(original)
    shutil.rmtree(proj / ".godot")
    rc, rep = preflight(g, "--preset", "macOS")
    check(rc == 1 and "import_incomplete" in codes(rep), f"an imported-nowhere project blocks export ({codes(rep)})")
    check(g.run(["--editor", "--import"]).returncode == 0, "re-import")
    wrapper_file = next((proj / "assets" / "prefabs").glob("*.tscn"))
    text = wrapper_file.read_text()
    wrapper_file.write_text(text + "\n; edited\n")
    rc, rep = preflight(g, "--preset", "macOS")
    check(rc == 1 and "wrapper_modified" in codes(rep), "an edited generated wrapper blocks export")
    wrapper_file.write_text(text)
    scene = proj / "bad.tscn"
    scene.write_text('[gd_scene load_steps=2 format=3]\n\n[ext_resource type="Script" path="res://addons/assetstudio/'
                     'editor/as_dock.gd" id="1"]\n\n[node name="X" type="Node"]\nscript = ExtResource("1")\n')
    rc, rep = preflight(g, "--preset", "macOS")
    check(rc == 1 and "forbidden_reference" in codes(rep), "a scene referencing an addon editor script blocks export")
    scene.unlink()
    rc, rep = preflight(g, "--preset", "macOS")
    check(rc == 0, f"clean again after the fixes ({codes(rep)})")
    (proj / "assetstudio.lock.json").rename(proj / "lock.bak")
    rc, rep = preflight(g, "--preset", "macOS")
    check(rc == 1 and "invalid_project_file" in codes(rep), "a missing lock is refused")
    (proj / "lock.bak").rename(proj / "assetstudio.lock.json")


def wrapper_scenarios(godot: str, g: C.Godot, proj: Path, tmp: Path, server: subprocess.Popen[str]) -> None:
    check = C.check
    out = tmp / "out" / "game.zip"
    p = wrapper(godot, proj, out, "--preflight-only")
    check(p.returncode == 0 and '"ok": true' in p.stdout and not out.exists(),
          f"wrapper --preflight-only passes ({p.stderr[-300:]})")
    victim = next((proj / "assets" / "library").rglob("portable.glb"))
    original = victim.read_bytes()
    victim.write_bytes(original + b"x")
    p = wrapper(godot, proj, out)
    check(p.returncode == 1 and not out.exists() and "refusing" in p.stderr,
          "wrapper refuses a tampered delivery and exports nothing")
    victim.write_bytes(original)
    server.terminate()
    server.wait(timeout=10)
    has_template = template_installed()
    p = wrapper(godot, proj, out, "--offline", *([] if has_template else ["--preflight-only"]))
    check(p.returncode == 0, f"wrapper --offline with the server stopped ({p.stderr[-400:]})")
    if not has_template:
        print("NOT RUN: godot export (macOS export template is not installed); preflight-only was run")
        return
    check(out.is_file(), "export wrote the archive")
    with zipfile.ZipFile(out) as zf:
        blob = b"".join(zf.read(n) for n in zf.namelist())
        names = " ".join(zf.namelist())
    check(SECRET.encode() not in blob and b".assetstudio" not in blob and b"server.token" not in blob
          and b"export_presets" not in blob and "token" not in names, "exported archive carries no private files")
    check(b"portable.glb" in blob, "exported archive carries the managed delivery")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--godot", default=shutil.which("godot") or "godot")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    token = "tok_" + secrets.token_hex(16)
    user_name = "assetstudio_exp_" + secrets.token_hex(4)
    tmp = Path(tempfile.mkdtemp(prefix="as_export_"))
    proj = tmp / "consumer"
    C.make_project(proj, user_name)
    token_file = tmp / "token.txt"
    token_file.write_text(token + "\n")
    token_file.chmod(0o600)
    g = C.Godot(args.godot, proj)
    server, port = C.start_server(token)
    try:
        prepare(g, proj, tmp, token_file, port)
        preflight_scenarios(g, proj)
        wrapper_scenarios(args.godot, g, proj, tmp, server)
    finally:
        server.terminate()
        server.wait(timeout=10)
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
            for d in C.user_dirs(user_name):
                shutil.rmtree(d, ignore_errors=True)
    C.check(not any(token in out for out in C.outputs), "bearer token never appears in Godot output")
    print(f"\n{'FAILED' if C.failures else 'ALL PASSED'}: {len(C.failures)} failure(s)")
    return 1 if C.failures else 0


if __name__ == "__main__":
    sys.exit(main())
