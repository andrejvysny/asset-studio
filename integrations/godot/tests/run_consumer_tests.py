#!/usr/bin/env python3
"""Consumer-project E2E for the AssetStudio addon CLI (AS-07a). No World Painter, no Terrain3D.

Creates a temp consumer project (minimal project.godot + a copy of addons/assetstudio), starts fake_server.py and runs:
connect -> add (primitive_prop v1 and v2) -> headless import -> wrapper check -> delete assets/library -> restore
(identical hashes) -> stop server -> verify --offline (0) and restore --offline -> tamper -> verify (1).

Usage: python3 integrations/godot/tests/run_consumer_tests.py [--godot PATH] [--keep]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
CONTRACTS = PROJECT.parents[1] / "contracts" / "godot-integration" / "v1"
SERVER_ID = "6f1c2a52-3c2e-4d4b-9a57-0b6f6f0c1d2e"
LIBRARY = "prj_0000000000000001"
ASSET = "ast_00000000000000aa"
VERSIONS = {"ver_00000000000000v1": "descriptors/valid/primitive_prop.json",
            "ver_00000000000000v2": "descriptors/valid/primitive_prop_v2.json"}
CLI = "res://addons/assetstudio/cli.gd"
TIMEOUT = 180

failures: list[str] = []
outputs: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)


def canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def tree_hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def last_line(p: subprocess.CompletedProcess[str]) -> str:
    lines = [ln for ln in p.stderr.strip().splitlines() if ln.strip()]
    return lines[0] if lines else ""


class Godot:
    def __init__(self, binary: str, project: Path) -> None:
        self.binary, self.project = binary, project

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run([self.binary, "--headless", "--path", str(self.project), *args], capture_output=True,
                              text=True, stdin=subprocess.DEVNULL, timeout=TIMEOUT)
        outputs.append(proc.stdout + proc.stderr)
        return proc

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run(["--script", CLI, "--", *args])


def make_project(root: Path, user_dir_name: str) -> None:
    root.mkdir(parents=True)
    (root / "project.godot").write_text(
        "config_version=5\n\n[application]\n\nconfig/name=\"AssetStudio Consumer E2E\"\n"
        "config/features=PackedStringArray(\"4.7\")\nconfig/use_custom_user_dir=true\n"
        f"config/custom_user_dir_name=\"{user_dir_name}\"\n")
    shutil.copytree(PROJECT / "addons" / "assetstudio", root / "addons" / "assetstudio")
    shutil.copy(HERE / "check_wrappers.gd", root / "check_wrappers.gd")


def user_dirs(name: str) -> list[Path]:
    home = Path.home()
    return [home / "Library" / "Application Support" / name,
            Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / name]


def start_server(token: str) -> tuple[subprocess.Popen[str], str]:
    server = subprocess.Popen(
        [sys.executable, str(HERE / "fake_server.py"), "0", "--contracts-dir", str(CONTRACTS)],
        stdout=subprocess.PIPE, text=True, env=dict(os.environ, ASSETSTUDIO_FAKE_TOKEN=token))
    line = server.stdout.readline().strip() if server.stdout else ""
    if not line.startswith("PORT "):
        raise RuntimeError("fake server failed to start")
    return server, line.split()[1]


def expected_anchors(lock: dict) -> list[str]:
    out = []
    for bid, b in lock["bindings"].items():
        version = lock["dependencies"][b["asset_key"]]["asset_ref"]["version_id"]
        desc = json.loads((CONTRACTS / "fixtures" / VERSIONS[version]).read_text())
        out.append(f"res://assets/prefabs/{bid}.tscn=" + ",".join(desc["placement_anchor"]))
    return sorted(out)


def usage_checks(g: Godot) -> None:
    check(g.cli().returncode == 2, "no command -> exit 2")
    check(g.cli("bogus").returncode == 2, "unknown command -> exit 2")
    check(g.cli("restore").returncode == 2, "restore without --locked -> exit 2")
    check(g.cli("verify", "--locked").returncode == 2, "verify without --offline -> exit 2")
    check(g.cli("connect", "--server-id", SERVER_ID, "--url", "http://127.0.0.1:1", "--token", "x").returncode == 2,
          "tokens are not accepted in argv -> exit 2")
    check(g.cli("add", "--library", LIBRARY, "--asset", ASSET, "--version", "ver_00000000000000v1",
                "--profile", "p", "--preserve").returncode == 2, "--profile with --preserve -> exit 2")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--godot", default=shutil.which("godot") or "godot")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    token = "tok_" + secrets.token_hex(16)
    user_name = "assetstudio_e2e_" + secrets.token_hex(4)
    tmp = Path(tempfile.mkdtemp(prefix="as_consumer_"))
    proj = tmp / "consumer"
    make_project(proj, user_name)
    token_file = tmp / "token.txt"
    token_file.write_text(token + "\n")
    token_file.chmod(0o600)
    g = Godot(args.godot, proj)
    server: subprocess.Popen[str] | None = None
    try:
        server, port = start_server(token)
        run_flow(g, proj, tmp, token_file, port)
        server.terminate()
        server.wait(timeout=10)
        server = None
        run_offline_flow(g, proj)
    finally:
        if server is not None:
            server.terminate()
            server.wait(timeout=10)
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
            for d in user_dirs(user_name):
                shutil.rmtree(d, ignore_errors=True)
    check(not any(token in out for out in outputs), "bearer token never appears in Godot output")
    check(not any("SCRIPT ERROR" in out for out in outputs), "no GDScript runtime errors in output")
    print(f"\n{'FAILED' if failures else 'ALL PASSED'}: {len(failures)} failure(s)")
    return 1 if failures else 0


def run_flow(g: Godot, proj: Path, tmp: Path, token_file: Path, port: str) -> None:
    usage_checks(g)
    url = f"http://127.0.0.1:{port}"
    p = g.cli("connect", "--server-id", SERVER_ID, "--url", url, "--token-file", str(token_file))
    check(p.returncode == 0, f"connect exit 0 ({p.stdout[-200:]}{p.stderr[-300:]})")
    check((proj / "assetstudio.project.json").exists(), "connect wrote assetstudio.project.json")
    for ver in VERSIONS:
        p = g.cli("add", "--library", LIBRARY, "--asset", ASSET, "--version", ver, "--preserve")
        check(p.returncode == 0, f"add {ver} exit 0 ({p.stdout[-200:]}{p.stderr[-300:]})")
    lock_raw = (proj / "assetstudio.lock.json").read_bytes()
    lock = json.loads(lock_raw)
    check(lock_raw == canonical(lock), "lock bytes are canonical (match the Python writer)")
    check(len(lock["bindings"]) == 2 and len(lock["dependencies"]) == 2, "two bindings, two dependencies")
    wrappers = sorted((proj / "assets" / "prefabs").glob("*.tscn"))
    check(len(wrappers) == 2, "two wrapper scenes written")
    state = json.loads((proj / ".assetstudio" / "state.json").read_text())
    check(sorted(state["pending_import"]) == sorted(lock["bindings"]), "bindings marked pending_import")
    check(not (proj / ".assetstudio" / "lock").exists(), "mutex released")
    before_import = tree_hashes(proj / "assets" / "library")
    check(g.cli("add", "--library", LIBRARY, "--asset", ASSET, "--version", "ver_00000000000000v1").returncode == 1,
          "adding the same exact version twice -> exit 1")
    p = g.run(["--editor", "--import"])
    check(p.returncode == 0, f"headless import exit 0 ({p.stderr[-300:]})")
    glbs = list((proj / "assets" / "library").rglob("portable.glb.import"))
    check(all("deduplicate_surfaces=false" in f.read_text() for f in glbs) and len(glbs) == 2,
          "pre-seeded import params survive Godot's import")
    p = g.run(["--script", "res://check_wrappers.gd", "--", *expected_anchors(lock)])
    check(p.returncode == 0 and "CHECK_OK" in p.stdout,
          f"wrappers load, Model at -anchor, mesh present ({p.stdout[-400:]})")
    p = g.cli("finalize")
    check(p.returncode == 0, "finalize stub exit 0")
    state = json.loads((proj / ".assetstudio" / "state.json").read_text())
    check(state["pending_import"] == [], "pending_import cleared")
    restore_flow(g, proj, lock, lock_raw, before_import)


def restore_flow(g: Godot, proj: Path, lock: dict, lock_raw: bytes, before_import: dict[str, str]) -> None:
    key = next(iter(lock["dependencies"]))
    bad = json.loads(lock_raw)
    bad["dependencies"][key]["deliveries"]["portable_glb_v1"]["delivery_id"] = "dlv_00000000000000d9"
    (proj / "assetstudio.lock.json").write_bytes(canonical(bad))
    p = g.cli("restore", "--locked")
    check(p.returncode == 1 and "integrity_mismatch" in p.stderr,
          f"restore refuses a lock whose delivery_id the server does not offer ({last_line(p)})")
    good = lock["dependencies"][key]["deliveries"]["portable_glb_v1"]
    bad["dependencies"][key]["deliveries"]["portable_glb_v1"] = good
    bad["dependencies"][key]["descriptor_sha256"] = "ab" * 32
    (proj / "assetstudio.lock.json").write_bytes(canonical(bad))
    p = g.cli("restore", "--locked")
    check(p.returncode == 1 and "integrity_mismatch" in p.stderr,
          f"restore refuses a locked descriptor sha256 mismatch ({last_line(p)})")
    (proj / "assetstudio.lock.json").write_bytes(lock_raw)
    shutil.rmtree(proj / "assets" / "library")
    p = g.cli("restore", "--locked")
    check(p.returncode == 0, f"restore --locked exit 0 ({p.stdout[-200:]}{p.stderr[-300:]})")
    check(tree_hashes(proj / "assets" / "library") == before_import,
          "restored files are byte-identical to the first install")
    check((proj / "assetstudio.lock.json").read_bytes() == lock_raw, "restore never rewrites the lock")


def run_offline_flow(g: Godot, proj: Path) -> None:
    p = g.cli("verify", "--locked", "--offline")
    check(p.returncode == 0,
          f"verify --locked --offline exit 0 with the server stopped ({p.stdout[-200:]}{p.stderr[-300:]})")
    before = tree_hashes(proj / "assets" / "library")
    shutil.rmtree(proj / "assets" / "library")
    check(g.cli("verify", "--locked", "--offline").returncode == 1, "verify fails when deliveries are missing")
    p = g.cli("restore", "--locked", "--offline")
    check(p.returncode == 0, f"restore --offline from the blob cache exit 0 ({p.stdout[-200:]}{p.stderr[-300:]})")
    check(tree_hashes(proj / "assets" / "library") == before, "offline restore is byte-identical too")
    victim = next((proj / "assets" / "library").rglob("portable.glb"))
    victim.write_bytes(victim.read_bytes() + b"x")
    p = g.cli("verify", "--locked", "--offline")
    check(p.returncode == 1 and "portable.glb" in p.stderr,
          f"tampered file -> verify exit 1 naming the file ({last_line(p)})")
    p = g.cli("restore", "--locked", "--offline")
    check(p.returncode == 1 and victim.read_bytes().endswith(b"x"), "restore reports tampering and never overwrites it")


if __name__ == "__main__":
    sys.exit(main())
