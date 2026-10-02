#!/usr/bin/env python3
"""Consumer-project E2E for the AssetStudio addon CLI (AS-07a). No World Painter, no Terrain3D.

Creates a temp consumer project (minimal project.godot + a copy of addons/assetstudio), starts fake_server.py and runs:
connect -> add (primitive_prop v1 and v2) -> headless import -> wrapper check -> delete assets/library -> restore
(identical hashes) -> stop server -> verify --offline (0) and restore --offline -> tamper -> verify (1).

AS-08 adds: material profiles (patch + material rules) applied at finalize, set-policy, profile-changed and
hand-edit conflict refusals, update (new binding and in-place) and rollback, restore from clean (identical hashes)
and a Python-side check of the lock.

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
    shutil.copy(HERE / "check_overrides.gd", root / "check_overrides.gd")
    write_profiles(root)


def write_profiles(root: Path) -> None:
    (root / "materials").mkdir()
    (root / "materials" / "lid_mat.tres").write_text(
        '[gd_resource type="StandardMaterial3D" format=3]\n\n[resource]\nalbedo_color = Color(0.2, 0.4, 0.6, 1)\n')
    profiles = root / "integration" / "material_profiles"
    profiles.mkdir(parents=True)
    lid = {"match": {"slot_id": "lid"}, "material": "res://materials/lid_mat.tres"}
    body = {"match": {"slot_id": "body"}, "patch": {"roughness": 0.25, "metallic": 0.1}}
    for pid, rules in (("mixed", [lid, body]), ("lid_only", [lid])):
        (profiles / f"{pid}.json").write_text(json.dumps({"schema_version": 1, "profile_id": pid, "rules": rules}))


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
    check(g.cli("set-policy", "--binding", "x").returncode == 2, "set-policy without a mode -> exit 2")
    check(g.cli("set-policy", "--binding", "x", "--preserve", "--override").returncode == 2,
          "set-policy with two modes -> exit 2")
    check(g.cli("update", "--binding", "x").returncode == 2, "update without --version -> exit 2")
    check(g.cli("rollback").returncode == 2, "rollback without --binding -> exit 2")


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
    check(p.returncode == 0, f"finalize exit 0 ({p.stdout[-200:]}{p.stderr[-300:]})")
    state = json.loads((proj / ".assetstudio" / "state.json").read_text())
    check(state["pending_import"] == [], "pending_import cleared")
    restore_flow(g, proj, lock, lock_raw, before_import)
    policy_flow(g, proj)


def read_lock(proj: Path) -> dict:
    return json.loads((proj / "assetstudio.lock.json").read_text())


def bindings_by_version(lock: dict) -> dict[str, str]:
    return {lock["dependencies"][b["asset_key"]]["asset_ref"]["version_id"]: bid for bid, b in lock["bindings"].items()}


def observe(g: Godot, *wrappers: str) -> dict:
    p = g.run(["--script", "res://check_overrides.gd", "--", *wrappers])
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("STATE ")), "")
    return json.loads(line[6:]) if line else {}


def wrapper_res(bid: str) -> str:
    return f"res://assets/prefabs/{bid}.tscn"


def wrapper_file(proj: Path, bid: str) -> Path:
    return proj / "assets" / "prefabs" / f"{bid}.tscn"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def override_nodes(state: dict, res: str) -> dict[str, list]:
    """mesh node path -> surfaces, only nodes that carry at least one override."""
    meshes = (state.get(res) or {}).get("meshes", {})
    return {k: v for k, v in meshes.items() if any(x is not None for x in v)}


def delivery_hashes(proj: Path) -> dict[str, str]:
    return {k: v for k, v in tree_hashes(proj / "assets" / "library").items()
            if k.endswith("receipt.json") or k.endswith("portable.glb")}


def import_and_finalize(g: Godot, label: str, *finalize_args: str) -> None:
    check(g.run(["--editor", "--import"]).returncode == 0, f"{label}: headless import")
    p = g.cli("finalize", *finalize_args)
    check(p.returncode == 0, f"{label}: finalize exit 0 ({p.stdout[-250:]}{p.stderr[-250:]})")


def policy_flow(g: Godot, proj: Path) -> None:
    """Profiles at finalize, set-policy, refusals, update (both modes), rollback, restore from clean."""
    lock = read_lock(proj)
    ids = bindings_by_version(lock)
    b1, b2 = ids["ver_00000000000000v1"], ids["ver_00000000000000v2"]
    check(g.run(["--editor", "--import"]).returncode == 0, "policy: reimport after restore")
    # --- set-policy: profile on the v1 binding (patch rule) and the v2 binding (material + patch rules) ---------
    p = g.cli("set-policy", "--binding", b1, "--profile", "mixed")
    check(p.returncode == 0, f"set-policy --profile mixed (v1) exit 0 ({p.stdout[-250:]}{p.stderr[-250:]})")
    p = g.cli("set-policy", "--binding", b2, "--profile", "mixed")
    check(p.returncode == 0 and "0 unmapped" in p.stdout, f"set-policy --profile mixed (v2): every slot mapped ({p.stdout[-250:]})")
    lock = read_lock(proj)
    mixed_sha = sha(proj / "integration" / "material_profiles" / "mixed.json")
    check(lock["bindings"][b1]["material_policy"] == {"mode": "project_mapping", "profile_id": "mixed",
                                                       "profile_sha256": mixed_sha}, "lock records profile id + sha256")
    check((proj / "assetstudio.lock.json").read_bytes() == canonical(lock), "lock canonical after set-policy")
    state = observe(g, wrapper_res(b1), wrapper_res(b2))
    v1_nodes, v2_nodes = override_nodes(state, wrapper_res(b1)), override_nodes(state, wrapper_res(b2))
    check(len(v1_nodes) == 1 and next(iter(v1_nodes.values()))[0]["roughness"] == 0.25
          and next(iter(v1_nodes.values()))[0]["metallic"] == 0.1, f"v1 body surface patched ({v1_nodes})")
    lids = [n for n, surf in v2_nodes.items() if surf[0]["albedo"] == "336699"]
    bodies = [n for n, surf in v2_nodes.items() if surf[0]["roughness"] == 0.25]
    check(len(v2_nodes) == 2 and len(lids) == 1 and len(bodies) == 1 and lids != bodies,
          f"v2: lid gets the material rule, body the patch ({v2_nodes})")
    check("editable path=\"Model\"" in wrapper_file(proj, b2).read_text(), "wrapper marks Model editable with overrides")
    check(abs(state[wrapper_res(b2)]["position"][2] + 0.5) < 1e-6, "Model still at -anchor")
    mixed_wrapper = sha(wrapper_file(proj, b2))
    # --- unmapped slots keep their source material -------------------------------------------------------------
    p = g.cli("set-policy", "--binding", b2, "--profile", "lid_only")
    check(p.returncode == 0 and "1 unmapped" in p.stdout, f"lid_only leaves one slot unmapped ({p.stdout[-200:]})")
    nodes = override_nodes(observe(g, wrapper_res(b2)), wrapper_res(b2))
    check(len(nodes) == 1 and next(iter(nodes.values()))[0]["albedo"] == "336699", "only the lid is overridden")
    p = g.cli("set-policy", "--binding", b2, "--preserve")
    check(p.returncode == 0, "set-policy --preserve")
    check(override_nodes(observe(g, wrapper_res(b2)), wrapper_res(b2)) == {}, "preserve: overrides are gone")
    check("editable" not in wrapper_file(proj, b2).read_text(), "preserve: no editable instance")
    p = g.cli("set-policy", "--binding", b2, "--override")
    check(p.returncode == 1 and "overrides" in p.stderr, f"override without its file -> exit 1 ({last_line(p)})")
    p = g.cli("set-policy", "--binding", b2, "--profile", "mixed")
    check(sha(wrapper_file(proj, b2)) == mixed_wrapper, "same inputs give the same wrapper bytes (deterministic)")
    override_flow(g, proj, b2)
    refusal_flow(g, proj, b1)
    update_flow(g, proj, b1, b2)
    clean_restore_flow(g, proj)


def override_flow(g: Godot, proj: Path, bid: str) -> None:
    odir = proj / "integration" / "material_profiles" / "overrides"
    odir.mkdir(parents=True)
    (odir / f"{bid}.json").write_text(json.dumps({"schema_version": 1, "profile_id": bid, "rules": [
        {"match": {"role": "surface"}, "patch": {"roughness": 0.9}}]}))
    p = g.cli("set-policy", "--binding", bid, "--override")
    check(p.returncode == 0, f"set-policy --override with its file ({p.stdout[-200:]}{p.stderr[-200:]})")
    policy = read_lock(proj)["bindings"][bid]["material_policy"]
    check(policy == {"mode": "override", "profile_id": None, "profile_sha256": None}, "override mode keeps profile fields null")
    nodes = override_nodes(observe(g, wrapper_res(bid)), wrapper_res(bid))
    check(len(nodes) == 2 and all(s[0]["roughness"] == 0.9 for s in nodes.values()), "override file applied to every role=surface slot")
    check(g.cli("set-policy", "--binding", bid, "--profile", "mixed").returncode == 0, "back to the mixed profile")


def refusal_flow(g: Godot, proj: Path, bid: str) -> None:
    profile = proj / "integration" / "material_profiles" / "mixed.json"
    original = profile.read_text()
    wrapper = wrapper_file(proj, bid)
    wrapper_before = wrapper.read_bytes()
    profile.write_text(json.dumps({"schema_version": 1, "profile_id": "mixed", "rules": [
        {"match": {"slot_id": "body"}, "patch": {"roughness": 0.6}}]}))
    p = g.cli("finalize", "--binding", bid)
    check(p.returncode == 1 and "profile_changed" in p.stderr, f"changed profile is refused without --reapply ({last_line(p)})")
    check(wrapper.read_bytes() == wrapper_before, "refused finalize leaves the wrapper untouched")
    check(g.cli("finalize", "--binding", bid, "--reapply").returncode == 0, "finalize --reapply accepts the new profile")
    new_sha = sha(profile)
    check(read_lock(proj)["bindings"][bid]["material_policy"]["profile_sha256"] == new_sha, "reapply updates profile_sha256")
    check(next(iter(override_nodes(observe(g, wrapper_res(bid)), wrapper_res(bid)).values()))[0]["roughness"] == 0.6,
          "the re-applied profile is in the wrapper")
    profile.write_text(original)
    check(g.cli("finalize", "--binding", bid, "--reapply").returncode == 0, "profile restored and re-applied")
    mine = wrapper.read_bytes()
    wrapper.write_bytes(mine + b"\n; hand edit\n")
    p = g.cli("finalize", "--binding", bid)
    check(p.returncode == 1 and "conflict" in p.stderr and wrapper.read_bytes().endswith(b"hand edit\n"),
          f"a hand-edited wrapper is a conflict and is never overwritten ({last_line(p)})")
    wrapper.write_bytes(mine)
    check(g.cli("finalize", "--binding", bid).returncode == 0, "finalize works again once the wrapper is the recorded one")
    check(g.cli("finalize", "--binding", "no-such-binding").returncode == 1, "unknown binding -> exit 1")


def update_flow(g: Godot, proj: Path, b1: str, b2: str) -> None:
    lock_before = (proj / "assetstudio.lock.json").read_bytes()
    w1 = sha(wrapper_file(proj, b1))
    new_id = "crate-v2"
    p = g.cli("update", "--binding", b1, "--version", "ver_00000000000000v2", "--new-binding", new_id)
    check(p.returncode == 0, f"update --new-binding exit 0 ({p.stdout[-200:]}{p.stderr[-300:]})")
    lock = read_lock(proj)
    check(set(lock["bindings"]) == {b1, b2, new_id}, "new binding added, old ones kept")
    check(lock["bindings"][new_id]["material_policy"] == lock["bindings"][b1]["material_policy"], "policy carried over")
    check(sha(wrapper_file(proj, b1)) == w1, "old binding's wrapper is intact")
    check(lock["dependencies"].keys() == read_lock_keys(lock_before), "old binding's dependency is untouched")
    import_and_finalize(g, "new-binding")
    nodes = override_nodes(observe(g, wrapper_res(new_id)), wrapper_res(new_id))
    check(len(nodes) == 2, f"new binding wrapper has the profile applied to v2 ({nodes})")
    # in-place update: b1 (v1) -> v2, then rollback
    lock_v1 = (proj / "assetstudio.lock.json").read_bytes()
    w1_final = sha(wrapper_file(proj, b1))
    p = g.cli("update", "--binding", b1, "--version", "ver_00000000000000v2")
    check(p.returncode == 0, f"update in place exit 0 ({p.stdout[-200:]}{p.stderr[-300:]})")
    lock = read_lock(proj)
    check(lock["bindings"][b1]["asset_key"] == lock["bindings"][b2]["asset_key"], "binding now points at v2")
    check(all(d["asset_ref"]["version_id"] == "ver_00000000000000v2" for d in lock["dependencies"].values()),
          "the v1 dependency was pruned: nothing references it")
    check((proj / "assetstudio.lock.json").read_bytes() == canonical(lock), "lock canonical after update")
    state = json.loads((proj / ".assetstudio" / "state.json").read_text())
    check(state["pending_import"] == [b1], "updated binding awaits finalize")
    import_and_finalize(g, "in-place update")
    nodes = override_nodes(observe(g, wrapper_res(b1)), wrapper_res(b1))
    check(len(nodes) == 2, "in-place update: profile re-applied on v2 (lid + body)")
    p = g.cli("rollback", "--binding", b1)
    check(p.returncode == 0, f"rollback exit 0 ({p.stdout[-200:]}{p.stderr[-300:]})")
    import_and_finalize(g, "rollback")
    check((proj / "assetstudio.lock.json").read_bytes() == lock_v1, "rollback restores the previous lock byte-for-byte")
    check(sha(wrapper_file(proj, b1)) == w1_final, "rollback regenerates the identical previous wrapper")
    check(g.cli("rollback", "--binding", b2).returncode == 1, "nothing to roll back -> exit 1")
    history = json.loads((proj / ".assetstudio" / "history.json").read_text())["entries"]
    kinds = [e.get("summary", {}).get("kind") for e in history]
    check("update" in kinds and "rollback" in kinds and "finalize" in kinds, f"history keeps summaries ({kinds})")


def read_lock_keys(raw: bytes) -> object:
    return json.loads(raw)["dependencies"].keys()


def clean_restore_flow(g: Godot, proj: Path) -> None:
    lock_raw = (proj / "assetstudio.lock.json").read_bytes()
    before = delivery_hashes(proj)
    shutil.rmtree(proj / "assets" / "library")
    p = g.cli("restore", "--locked")
    check(p.returncode == 0, f"restore from clean exit 0 ({p.stdout[-200:]}{p.stderr[-200:]})")
    check(delivery_hashes(proj) == before and len(before) == 4, "restore from clean gives identical delivery hashes")
    check((proj / "assetstudio.lock.json").read_bytes() == lock_raw, "restore leaves the lock byte-identical")
    check(g.cli("verify", "--locked", "--offline").returncode == 0, "verify passes after the clean restore")
    python_lock_check(lock_raw)


def python_lock_check(raw: bytes) -> None:
    code = ("import sys; from assetstudio_core.project_lock import parse_lock, lock_bytes; raw = sys.stdin.buffer.read();"
            " sys.exit(0 if lock_bytes(parse_lock(raw)) == raw else 3)")
    try:
        p = subprocess.run(["uv", "run", "python", "-c", code], input=raw, capture_output=True, cwd=PROJECT.parents[1],
                           timeout=120)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("SKIP python lock validation (uv unavailable)")
        return
    check(p.returncode == 0, f"lock parses and re-serializes identically with the Python validator ({p.stderr[-200:]!r})")


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
