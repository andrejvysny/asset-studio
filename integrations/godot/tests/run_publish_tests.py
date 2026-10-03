#!/usr/bin/env python3
"""Publish E2E for the AssetStudio addon (AS-09) in a temp consumer project against fake_server.py.

Scenes equal to the fixtures primitive_prop / csg_hut / textured_tree / custom_shader_crystal / vertex_color_rock are
published with the CLI (`publish`, preview only). Each build directory is checked with the SERVER's own publication
checks (publish_check.py via `uv run`) and the portable GLB is re-imported in Godot (check_publish.gd). Scripted,
animated, custom-class, camera, missing-dependency and unsupported-file scenes must be blocked before any upload.
Then: preview -> commit, journal replay, new version with compare-and-swap, stale_pointer, lost commit response
(recovery through publication-operations), admission refusal, and "the project is unchanged".

Usage: python3 integrations/godot/tests/run_publish_tests.py [--godot PATH] [--keep]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from run_consumer_tests import CLI, CONTRACTS, HERE, LIBRARY, PROJECT, SERVER_ID, start_server, user_dirs

ROOT = PROJECT.parents[1]
FIXTURES = CONTRACTS / "fixtures" / "source_packages" / "valid"
TIMEOUT = 240
SCENES = {"prop": "primitive_prop", "hut": "csg_hut", "tree": "textured_tree", "crystal": "custom_shader_crystal",
          "rock": "vertex_color_rock_with_collision", "array_mesh": "array_mesh_prop"}
ASSET, V2 = "ast_00000000000000aa", "ver_00000000000000v2"
DEP_KEY = "3e4cecabdb6c9bac29d5f9c655852e10f1d96abdb437cee386bfecf3974b1cbf"
DEP_MSHA = "3af631546729db64d1b19e56406e7c5f1787f2801e1fa953c86e467714c4f011"
DEP_DIR = f"assets/library/{DEP_KEY}/{DEP_MSHA}"
failures: list[str] = []
OUTPUT: list[str] = []
VERDICTS: dict[str, dict] = {}


def check(cond: bool, label: str) -> None:
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)


def tree_hashes(root: Path) -> dict[str, str]:
    skip = {".assetstudio", ".godot"}
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and not skip & set(p.relative_to(root).parts)}


def jsons(text: str) -> list[dict]:
    """Every top-level JSON object printed on stdout (the CLI prints the review, then the outcome)."""
    out, dec, i = [], json.JSONDecoder(), text.find("{")
    while 0 <= i < len(text):
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            break
        out.append(obj)
        i = text.find("{", end)
    return out


class Env:
    def __init__(self, godot: str, project: Path, port: str, token: str) -> None:
        self.godot, self.project, self.port, self.token = godot, project, port, token

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run([self.godot, "--headless", "--path", str(self.project), *args], capture_output=True,
                              text=True, stdin=subprocess.DEVNULL, timeout=TIMEOUT)
        OUTPUT.append(proc.stdout + proc.stderr)
        return proc

    def publish(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run(["--script", CLI, "--", "publish", "--library", LIBRARY, *args])

    def control(self, path: str, body: dict | None = None) -> object:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def previews(self) -> int:
        return sum(1 for e in self.control("/__log") if e["path"].endswith("publications:preview"))  # type: ignore

    def published(self) -> list[dict]:
        return self.control("/__publications")  # type: ignore


def make_project(root: Path, user_dir_name: str) -> None:
    root.mkdir(parents=True)
    (root / "project.godot").write_text(
        "config_version=5\n\n[application]\n\nconfig/name=\"AssetStudio Publish E2E\"\n"
        "config/features=PackedStringArray(\"4.7\")\nconfig/use_custom_user_dir=true\n"
        f"config/custom_user_dir_name=\"{user_dir_name}\"\n")
    shutil.copytree(PROJECT / "addons" / "assetstudio", root / "addons" / "assetstudio")
    shutil.copy(HERE / "check_publish.gd", root / "check_publish.gd")
    for fixture in SCENES.values():
        with zipfile.ZipFile(FIXTURES / f"{fixture}.zip") as z:
            for name in z.namelist():
                if name != "source_manifest.json":
                    z.extract(name, root)
    write_bad_scenes(root)
    write_managed_dependency(root)


def write_managed_dependency(root: Path) -> None:
    """An installed portable delivery (lock + files) that scenes/cluster.tscn instances twice."""
    glb = root / DEP_DIR / "portable.glb"
    glb.parent.mkdir(parents=True)
    shutil.copy(CONTRACTS / "fixtures" / "glb" / "primitive_prop.portable.glb", glb)
    lock = json.loads((CONTRACTS / "fixtures" / "locks" / "valid" / "cluster_requires.json").read_text())
    dep = lock["dependencies"][DEP_KEY]
    lock.update(bindings={}, roots=[], dependencies={DEP_KEY: dep})
    dep["deliveries"]["portable_glb_v1"]["manifest_sha256"] = DEP_MSHA
    (root / "assetstudio.lock.json").write_text(
        json.dumps(lock, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    path = f"res://{DEP_DIR}/portable.glb"
    (root / "scenes" / "cluster.tscn").write_text(
        '[gd_scene load_steps=3 format=3]\n\n'
        f'[ext_resource type="PackedScene" path="{path}" id="1_prop"]\n\n'
        '[sub_resource type="BoxMesh" id="BoxMesh_base"]\nsize = Vector3(4, 0.05, 2)\n\n'
        '[node name="PropCluster" type="Node3D"]\n\n'
        '[node name="Base" type="MeshInstance3D" parent="."]\nmesh = SubResource("BoxMesh_base")\n\n'
        '[node name="PropA" parent="." instance=ExtResource("1_prop")]\n\n'
        '[node name="PropB" parent="." instance=ExtResource("1_prop")]\n\n'
        '[node name="GroundAnchor" type="Marker3D" parent="."]\n')


def glb_with(extra: dict) -> bytes:
    """A GLB container whose JSON chunk carries `extra` top-level keys (skins, animations, ...)."""
    doc = json.dumps({"asset": {"version": "2.0"}, **extra}, separators=(",", ":")).encode()
    doc += b" " * (-len(doc) % 4)
    body = struct.pack("<I4s", len(doc), b"JSON") + doc
    return struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body


def write_bad_scenes(root: Path) -> None:
    (root / "scripts").mkdir()
    (root / "scripts" / "s.gd").write_text("extends Node3D\n")
    box = '[sub_resource type="BoxMesh" id="BoxMesh_1"]\n\n'
    body = '[node name="Body" type="MeshInstance3D" parent="."]\nmesh = SubResource("BoxMesh_1")\n'
    head = '[gd_scene load_steps=3 format=3]\n\n'
    scenes = {
        "scripted": head + '[ext_resource type="Script" path="res://scripts/s.gd" id="1_s"]\n\n' + box
        + '[node name="Scripted" type="Node3D"]\nscript = ExtResource("1_s")\n\n' + body,
        "animated": head + box + '[node name="Animated" type="Node3D"]\n\n' + body
        + '\n[node name="Anim" type="AnimationPlayer" parent="."]\n',
        "custom": head + box + '[node name="Custom" type="MyProp"]\n\n' + body,
        "camera": head + box + '[node name="Cam" type="Node3D"]\n\n' + body
        + '\n[node name="Camera" type="Camera3D" parent="."]\n',
        "missing": head + '[ext_resource type="Texture2D" path="res://textures/gone.png" id="1_t"]\n\n' + box
        + '[node name="Missing" type="Node3D"]\n\n' + body,
        "audio": head + '[ext_resource type="AudioStream" path="res://a.ogg" id="1_a"]\n\n' + box
        + '[node name="Audio" type="Node3D"]\n\n' + body,
    }
    (root / "models").mkdir(exist_ok=True)
    (root / "models" / "skinned.glb").write_bytes(glb_with({"skins": [{"joints": []}], "animations": [{"channels": []}]}))
    scenes["skinned"] = (head + '[ext_resource type="PackedScene" path="res://models/skinned.glb" id="1_m"]\n\n'
                         + '[node name="Skinned" type="Node3D"]\n\n[node name="Model" parent="." instance=ExtResource("1_m")]\n')
    (root / "scenes" / "stale.tscn").write_text(
        '[gd_scene load_steps=3 format=3]\n\n[ext_resource type="StandardMaterial3D" uid="uid://b3k1m0pa1n7ro" '
        'path="res://moved/prop_mat.tres" id="1_mat"]\n\n' + box + '[node name="Stale" type="Node3D"]\n\n'
        + body.replace('mesh = SubResource("BoxMesh_1")', 'mesh = SubResource("BoxMesh_1")\nsurface_material_override/0 = ExtResource("1_mat")'))
    (root / "a.ogg").write_bytes(b"OggS")
    for name, text in scenes.items():
        (root / "scenes" / f"{name}.tscn").write_text(text)


def build_checks(env: Env) -> dict[str, Path]:
    dirs: dict[str, Path] = {}
    for name in SCENES:
        out = env.project / ".assetstudio" / "publish" / f"build_{name}"
        p = env.publish("--scene", f"res://scenes/{name}.tscn", "--out", str(out))
        reviews = jsons(p.stdout)
        check(p.returncode == 0 and len(reviews) == 1 and reviews[0]["phase"] == "previewed",
              f"{name}: preview only (exit {p.returncode}) {p.stderr[-300:] if p.returncode else ''}")
        ref = subprocess.run(["uv", "run", "python", str(HERE / "publish_check.py"), str(out)], cwd=ROOT,
                             capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=TIMEOUT)
        verdict = json.loads(ref.stdout.strip().splitlines()[-1]) if ref.stdout.strip() else {}
        check(ref.returncode == 0 and verdict.get("ok"), f"{name}: the server's own publication checks accept it {verdict if not verdict.get('ok') else ''}")
        dirs[name] = out
        if verdict.get("ok"):
            check(set(verdict["detected"]) <= set(verdict["declared"]), f"{name}: detected capabilities are declared")
            VERDICTS[name] = verdict
    return dirs


def content_checks(env: Env, dirs: dict[str, Path]) -> None:
    verdicts = VERDICTS
    p = env.run(["--script", "res://check_publish.gd", "--", *[str(d / "portable.glb") for d in dirs.values()]])
    facts = {Path(f["path"]).parent.name.removeprefix("build_"): f for f in
             (json.loads(line.split("GLBFACTS ", 1)[1]) for line in p.stdout.splitlines() if line.startswith("GLBFACTS "))}
    check(set(facts) == set(dirs), "every portable.glb re-imports")
    prop, hut, tree, crystal, rock = (facts.get(n, {}) for n in ("prop", "hut", "tree", "crystal", "rock"))
    check(prop.get("mesh_nodes") == 1 and len(prop["surfaces"]) == 1 and prop["surfaces"][0]["normal"]
          and prop["surfaces"][0]["uv"], "prop: one mesh, normals and UVs")
    check(hut.get("mesh_nodes") == 1 and len(hut["surfaces"]) == 3, "csg hut: baked to ONE mesh with 3 surfaces")
    check(tree.get("mesh_nodes") == 2 and all(s["texture"] for s in tree["surfaces"])
          and any(s["transparency"] != 0 for s in tree["surfaces"]), "tree: two meshes, textures and alpha mode kept")
    check(len(verdicts["tree"]["slots"]) == 2, "tree: two stable material slots")
    check(rock.get("surfaces") and rock["surfaces"][0]["color"], "rock: vertex colors survive")
    check(crystal.get("surfaces") and crystal["surfaces"][0]["material"] == "StandardMaterial3D"
          and crystal["surfaces"][0]["albedo"] == "66ccff", "crystal: ShaderMaterial replaced by a tinted approximation")
    check("custom_shader_approximated" in verdicts["crystal"]["warnings"] and verdicts["crystal"]["status"] == "approximated",
          "crystal: the shader approximation is disclosed")
    check(verdicts["rock"]["declared"].count("static_collision") == 1, "rock: static collision declared")
    array_checks(dirs["array_mesh"], facts.get("array_mesh", {}))
    for name, d in dirs.items():
        with zipfile.ZipFile(d / "source.zip") as z:
            infos = z.infolist()
            check(all(i.date_time == (1980, 1, 1, 0, 0, 0) and i.compress_type in (0, 8) for i in infos)
                  and [i.filename for i in infos] == sorted(i.filename for i in infos), f"{name}: deterministic zip layout")
            manifest = json.loads(z.read("source_manifest.json"))
        check(sorted(manifest) == sorted(["schema_version", "entry_scene", "source_godot_version", "files", "resource_map",
                                          "asset_dependencies", "capabilities", "conversion_report", "placement"]),
              f"{name}: manifest top-level names are exact")


def array_checks(out: Path, facts: dict) -> None:
    """The scene is Godot 4.7.2's own output (text format=4, embedded ArrayMesh, external Material .tres)."""
    check(facts.get("mesh_nodes") == 1 and len(facts.get("surfaces", [])) == 1 and facts["surfaces"][0]["normal"]
          and facts["surfaces"][0]["uv"], "array_mesh: format=4 ArrayMesh exports one mesh with normals and UVs")
    with zipfile.ZipFile(out / "source.zip") as z:
        scene = z.read("scenes/array_mesh.tscn")
        names = z.namelist()
    check(scene.startswith(b"[gd_scene format=4]") and b'type="Material"' in scene,
          "array_mesh: the published scene is the format=4 text, byte-for-byte")
    check("materials/array_mat.tres" in names, "array_mesh: the external Material .tres is packaged")


def dependency_checks(env: Env) -> None:
    out = env.project / ".assetstudio" / "publish" / "build_cluster"
    p = env.publish("--scene", "res://scenes/cluster.tscn", "--out", str(out))
    check(p.returncode == 0, f"cluster: instances of an installed delivery publish ({p.stderr[-200:] if p.returncode else ''})")
    with zipfile.ZipFile(out / "source.zip") as z:
        manifest = json.loads(z.read("source_manifest.json"))
    entry = manifest["resource_map"].get(f"res://{DEP_DIR}/portable.glb", {})
    check(entry.get("kind") == "asset_dependency" and entry.get("asset_key") == DEP_KEY
          and entry.get("entrypoint") == "portable.glb", "cluster: the delivery is an asset_dependency, not a package file")
    dep = manifest["asset_dependencies"].get(DEP_KEY, {})
    check(dep.get("representation") == "portable_glb_v1" and dep.get("delivery_id") == "dlv_00000000000000d1"
          and dep.get("asset_ref", {}).get("asset_id") == ASSET, "cluster: the dependency pins the locked ref and delivery")
    check([f["path"] for f in manifest["files"]] == ["scenes/cluster.tscn"], "cluster: the delivery's files are not copied")
    ref = subprocess.run(["uv", "run", "python", str(HERE / "publish_check.py"), str(out)], cwd=ROOT, capture_output=True,
                         text=True, stdin=subprocess.DEVNULL, timeout=TIMEOUT)
    check(ref.returncode == 0, f"cluster: the server's checks accept it ({ref.stdout[-300:] if ref.returncode else ''})")
    lock_path = env.project / "assetstudio.lock.json"
    saved = lock_path.read_bytes()
    lock_path.unlink()
    p = env.publish("--scene", "res://scenes/cluster.tscn", "--out", str(out))
    lock_path.write_bytes(saved)
    check(p.returncode == 1 and "assetstudio.lock.json" in p.stderr, "cluster: a delivery that is not in the lock blocks publication")


def stale_uid_checks(env: Env) -> None:
    out = env.project / ".assetstudio" / "publish" / "build_stale"
    p = env.publish("--scene", "res://scenes/stale.tscn", "--out", str(out))
    check(p.returncode == 0, f"stale path + valid uid: resolved through the uid ({p.stderr[-200:] if p.returncode else ''})")
    with zipfile.ZipFile(out / "source.zip") as z:
        manifest = json.loads(z.read("source_manifest.json"))
    entry = manifest["resource_map"].get("res://moved/prop_mat.tres", {})
    check(entry.get("path") == "materials/prop_mat.tres" and entry.get("original_uid") == "uid://b3k1m0pa1n7ro",
          "stale: resource_map keeps the written path and maps it to the real file")
    ref = subprocess.run(["uv", "run", "python", str(HERE / "publish_check.py"), str(out)], cwd=ROOT, capture_output=True,
                         text=True, stdin=subprocess.DEVNULL, timeout=TIMEOUT)
    check(ref.returncode == 0, f"stale: the server's checks accept it ({ref.stdout[-300:] if ref.returncode else ''})")


def determinism_checks(env: Env, dirs: dict[str, Path]) -> None:
    for name in ("tree", "hut"):
        before = hashlib.sha256((dirs[name] / "source.zip").read_bytes()).hexdigest()
        glb = hashlib.sha256((dirs[name] / "portable.glb").read_bytes()).hexdigest()
        env.publish("--scene", f"res://scenes/{name}.tscn", "--out", str(dirs[name]))
        check(hashlib.sha256((dirs[name] / "source.zip").read_bytes()).hexdigest() == before, f"{name}: package is byte-identical on rebuild")
        check(hashlib.sha256((dirs[name] / "portable.glb").read_bytes()).hexdigest() == glb, f"{name}: portable.glb is byte-identical on rebuild")


def blocked_checks(env: Env) -> None:
    before = env.previews()
    expect = {"scripted": "scripts are not supported", "animated": "AnimationPlayer", "custom": "MyProp",
              "camera": "Camera3D", "missing": "does not exist", "audio": "AudioStream",
              "skinned": "contains skins"}
    for name, needle in expect.items():
        p = env.publish("--scene", f"res://scenes/{name}.tscn")
        check(p.returncode == 1 and needle in p.stderr, f"blocked: {name} (exit {p.returncode}; wants '{needle}')")
    check(env.previews() == before, "blocked scenes are never uploaded")
    p = env.publish("--scene", "res://scenes/nope.tscn")
    check(p.returncode == 1 and "save the scene first" in p.stderr, "a scene that is not saved is refused")


def commit_checks(env: Env) -> None:
    env.control("/__publish_reset", {})
    p = env.publish("--scene", "res://scenes/prop.tscn", "--commit", "--name", "Prop One", "--tags", "a,b", "--licence", "CC0")
    docs = jsons(p.stdout)
    check(p.returncode == 0 and len(docs) == 2 and docs[1]["phase"] == "committed", "new asset: preview then commit")
    done = env.published()
    check(len(done) == 1 and done[0]["name"] == "Prop One" and done[0]["tags"] == ["a", "b"] and done[0]["licence"] == "CC0",
          "the commit carries name, tags and licence")
    p = env.publish("--scene", "res://scenes/prop.tscn", "--commit", "--name", "Prop One", "--tags", "a,b", "--licence", "CC0")
    check(p.returncode == 0 and "already_published" in p.stdout and len(env.published()) == 1,
          "the same reviewed publication is answered from the journal, not published twice")
    p = env.publish("--scene", "res://scenes/prop.tscn", "--new-version-of", ASSET, "--expected-current", V2, "--commit")
    docs = jsons(p.stdout)
    check(p.returncode == 0 and docs[0]["target"]["mode"] == "new_version" and len(env.published()) == 2
          and env.published()[1]["asset_id"] == ASSET, "new version of an existing asset with the expected base version")
    p = env.publish("--scene", "res://scenes/hut.tscn", "--new-version-of", ASSET, "--expected-current", V2, "--commit")
    check(p.returncode == 1 and "conflict" in p.stderr and "untouched" in p.stderr and len(env.published()) == 2,
          "stale base version -> stale_pointer conflict, nothing published")


def recovery_checks(env: Env) -> None:
    env.control("/__scenario", {"name": "drop_commit_response"})
    p = env.publish("--scene", "res://scenes/tree.tscn", "--commit", "--name", "Tree Lost Response")
    docs = jsons(p.stdout)
    env.control("/__scenario", {"name": "normal"})
    done = [d for d in env.published() if d["name"] == "Tree Lost Response"]
    check(p.returncode == 0 and len(docs) == 2 and docs[1]["outcome"]["via"] == "operation_query" and len(done) == 1,
          "lost commit response: recovered through the operation query, exactly one version")
    log = env.control("/__log")
    check(any("publication-operations/" in e["path"] for e in log), "the operation endpoint was queried")  # type: ignore
    env.control("/__scenario", {"name": "preview_busy"})
    p = env.publish("--scene", "res://scenes/rock.tscn", "--fresh")
    env.control("/__scenario", {"name": "normal"})
    check(p.returncode == 1 and "staging_capacity" in p.stderr, "admission refusal names its reason")
    p = env.publish("--scene", "res://scenes/rock.tscn", "--commit", "--new-version-of", ASSET)
    check(p.returncode == 2, "--new-version-of without --expected-current is a usage error")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--godot", default=shutil.which("godot") or "godot")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    token = "tok_" + secrets.token_hex(16)
    user_name = "assetstudio_publish_" + secrets.token_hex(4)
    tmp = Path(tempfile.mkdtemp(prefix="as_publish_"))
    server = None
    try:
        proj = tmp / "project"
        make_project(proj, user_name)
        server, port = start_server(token)
        token_file = tmp / "token.txt"
        token_file.write_text(token + "\n")
        token_file.chmod(0o600)
        env = Env(args.godot, proj, port, token)
        env.run(["--import"])
        p = env.run(["--script", CLI, "--", "connect", "--server-id", SERVER_ID, "--url", f"http://127.0.0.1:{port}",
                    "--token-file", str(token_file)])
        check(p.returncode == 0, "connect")
        before = tree_hashes(proj)
        dirs = build_checks(env)
        content_checks(env, dirs)
        determinism_checks(env, dirs)
        dependency_checks(env)
        stale_uid_checks(env)
        blocked_checks(env)
        commit_checks(env)
        recovery_checks(env)
        check(not any("SCRIPT ERROR" in o or "Parse Error" in o for o in OUTPUT), "no script errors in any Godot output")
        check(token not in "".join(OUTPUT), "the bearer token never appears in output")
        check(tree_hashes(proj) == before, "the project (scenes, resources, lock, config) is unchanged by publishing")
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


if __name__ == "__main__":
    sys.exit(main())
