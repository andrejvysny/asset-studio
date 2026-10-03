#!/usr/bin/env python3
"""godot_static_source_v1 import test (AS-07b) in a temp consumer project; no server needed.

Installs every valid source package fixture (and the portable dependency of prop_cluster) from the fixture zips with
the addon installer, runs the headless import on a FRESH `.godot`, loads every derived entry scene and checks that
meshes, materials, textures, shaders and the instanced dependency resolve, that no original UID is registered (two
versions sharing UIDs coexist) and that the import prints no UID warning or error. Then `.godot` is deleted and the
project is imported and checked again.

Usage: python3 integrations/godot/tests/run_source_tests.py [--godot PATH] [--keep]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
CONTRACTS = PROJECT.parents[1] / "contracts" / "godot-integration" / "v1"
TIMEOUT = 240
BAD_OUTPUT = ("SCRIPT ERROR", "Parse Error", "Failed to load script", "Compile Error", "PROBE_FAILED")
UID_NOISE = ("uid", "duplicate")

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)


def run(godot: str, project: Path, args: list[str]) -> tuple[int, str]:
    proc = subprocess.run([godot, "--headless", "--path", str(project), *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=TIMEOUT)
    return proc.returncode, proc.stdout + proc.stderr


def make_project(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "project.godot").write_text(
        "config_version=5\n\n[application]\n\nconfig/name=\"AssetStudio Source E2E\"\nconfig/features=PackedStringArray(\"4.7\")\n")
    shutil.copytree(PROJECT / "addons" / "assetstudio", root / "addons" / "assetstudio")
    for name in ("source_install_probe.gd", "check_source_scenes.gd"):
        shutil.copy(HERE / name, root / name)


def noise(output: str) -> list[str]:
    """Import/load output lines that mention a UID problem."""
    return [ln for ln in output.splitlines()
            if any(w in ln.lower() for w in UID_NOISE) and ("error" in ln.lower() or "warning" in ln.lower())]


def state_of(output: str) -> dict:
    line = next((ln for ln in output.splitlines() if ln.startswith("STATE ")), "")
    return json.loads(line[6:]) if line else {}


def verify_scenes(state: dict, label: str) -> None:
    scenes = state.get("scenes", {})
    check(set(scenes) == {"primitive_prop", "primitive_prop_v2", "csg_hut", "textured_tree", "custom_shader_crystal",
                          "prop_cluster", "vertex_color_rock_with_collision", "array_mesh_prop"},
          f"{label}: all eight entry scenes load")
    check(all("error" not in s for s in scenes.values()), f"{label}: no scene failed to load")
    check(state.get("foreign_uids_registered") == [], f"{label}: no original (foreign) UID is registered")
    v1 = scenes.get("primitive_prop", {}).get("nodes", {})
    v2 = scenes.get("primitive_prop_v2", {}).get("nodes", {})
    check(v1.get("Body", {}).get("mesh") == "BoxMesh" and v1["Body"]["materials"][0]["albedo"] == "cc4d33",
          f"{label}: v1 mesh and package-file material resolve ({v1.get('Body')})")
    check(v2.get("Crate", {}).get("materials", [{}])[0].get("albedo") == "8c6133"
          and v2.get("Lid", {}).get("materials", [{}])[0].get("albedo") == "594026",
          f"{label}: v2 resolves its own materials side by side with v1")
    hut = scenes.get("csg_hut", {}).get("nodes", {})
    check(hut.get("Shell/Walls", {}).get("type") == "CSGBox3D" and hut.get("Shell/Roof", {}).get("type") == "CSGCylinder3D",
          f"{label}: csg_hut instantiates its CSG tree")
    tree = scenes.get("textured_tree", {}).get("nodes", {})
    check(tree.get("Trunk", {}).get("materials", [{}])[0].get("texture", 0) > 0
          and tree.get("Crown", {}).get("materials", [{}])[0].get("texture", 0) > 0,
          f"{label}: textured_tree materials resolve their relocated textures")
    crystal = scenes.get("custom_shader_crystal", {}).get("nodes", {})
    check(crystal.get("Gem", {}).get("materials", [{}])[0].get("shader_code") is True,
          f"{label}: crystal ShaderMaterial loads its relocated shader")
    cluster = scenes.get("prop_cluster", {}).get("nodes", {})
    check(any(k.startswith("PropA/") and v.get("type") == "MeshInstance3D" for k, v in cluster.items())
          and any(k.startswith("PropB/") for k in cluster), f"{label}: prop_cluster instances the installed portable entrypoint")
    rock = scenes.get("vertex_color_rock_with_collision", {}).get("nodes", {})
    check(rock.get("Body/Shape", {}).get("shape") == "BoxShape3D" and any(k.startswith("Mesh/") for k in rock),
          f"{label}: rock glb instance and collision shape")


def verify_array_mesh(state: dict, label: str) -> None:
    body = state.get("scenes", {}).get("array_mesh_prop", {}).get("nodes", {}).get("Body", {})
    surf = (body.get("array_surfaces") or [{}])[0]
    check(body.get("mesh") == "ArrayMesh" and surf.get("vertices") == 24
          and (surf.get("material") or {}).get("albedo") == "3380cc",
          f"{label}: format=4 ArrayMesh scene loads with its relocated external Material ({body})")


def import_and_check(godot: str, proj: Path, label: str) -> None:
    code, out = run(godot, proj, ["--editor", "--import"])
    check(code == 0, f"{label}: headless import exit 0")
    check(not any(b in out for b in BAD_OUTPUT), f"{label}: no script errors during import")
    check(not noise(out), f"{label}: import prints no UID warning or error ({noise(out)[:2]})")
    code, out = run(godot, proj, ["--script", "res://check_source_scenes.gd"])
    check(code == 0 and not any(b in out for b in BAD_OUTPUT), f"{label}: scenes load without script errors")
    check(not noise(out), f"{label}: loading prints no UID warning or error ({noise(out)[:2]})")
    verify_scenes(state_of(out), label)
    verify_array_mesh(state_of(out), label)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--godot", default=shutil.which("godot") or "godot")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="as_source_"))
    proj = tmp / "consumer"
    try:
        make_project(proj)
        code, out = run(args.godot, proj, ["--script", "res://source_install_probe.gd", "--", str(CONTRACTS)])
        check(code == 0 and "INSTALLED 8" in out, f"install probe installs eight packages ({out[-300:]})")
        check((proj / ".godot").exists() is False, "project starts without .godot (fresh import)")
        import_and_check(args.godot, proj, "fresh import")
        shutil.rmtree(proj / ".godot")
        import_and_check(args.godot, proj, "reimport after deleting .godot")
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"kept {tmp}")
    print(f"\n{'FAILED' if failures else 'ALL PASSED'}: {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
