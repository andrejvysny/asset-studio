"""Static scene structure: cycle rejection, surface and collision proof against generated source packages."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from assetstudio_core.delivery import AssetDescriptorV1
from assetstudio_core.source_manifest import SourcePackageManifestV1, source_manifest_bytes
from assetstudio_processing.source_package import SourcePackageReport, validate_source_package
from assetstudio_server.services.principals import ServiceError
from assetstudio_server.services.source_publication_checks import SourceFacts, structure_evidence

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from integration_fixture_packages import BASE, EXACT, build_package_doc, zip_of  # noqa: E402
from integration_fixture_sources import rock_files  # noqa: E402
from make_integration_fixtures import descriptor_docs  # noqa: E402

CAPS = json.loads((ROOT / "contracts/godot-integration/v1/capabilities.json").read_text())
HEAD = '[gd_scene format=3]\n\n'
BOX = '[sub_resource type="BoxMesh" id="m"]\n\n'
ROOT_NODE = '[node name="Root" type="Node3D"]\n\n'
MESH_BODY = '[node name="Body" type="MeshInstance3D" parent="."]\nmesh = SubResource("m")\n\n'


def _ext(path: str, rid: str = "1") -> str:
    return f'[ext_resource type="PackedScene" path="res://{path}" id="{rid}"]\n'


def _scene(*parts: str) -> bytes:
    return "".join(parts).encode()


def _build(tmp: Path, files: dict[str, bytes], node: str = "Body", surface: int = 0,
           caps: list[str] | None = None, entry: str = "scenes/main.tscn") -> tuple[SourcePackageReport, SourceFacts]:
    desc = AssetDescriptorV1.model_validate(descriptor_docs()["primitive_prop"])
    doc = build_package_doc(files, entry, desc, caps or BASE, EXACT)
    doc["placement"]["material_slots"][0]["source_surfaces"] = [{"node_path": node, "surface": surface}]
    manifest = SourcePackageManifestV1.model_validate(doc)
    zip_path = tmp / "pkg.zip"
    zip_path.write_bytes(zip_of(source_manifest_bytes(manifest), files))
    report = validate_source_package(zip_path, tmp / "staging", capabilities=CAPS)
    return report, SourceFacts(report, manifest, [])


def _details(report: SourcePackageReport) -> list[str]:
    return [e.detail for e in report.errors]


def _evidence(facts: SourceFacts, collision: tuple[int, list[str]] | None = None) -> dict[str, Any]:
    claim = SimpleNamespace(shape_count=collision[0], shape_types=collision[1]) if collision else None
    return structure_evidence(SimpleNamespace(collision=claim), facts)  # type: ignore[arg-type]


def _body(extra: str = "") -> bytes:
    return _scene(HEAD, BOX, ROOT_NODE, '[node name="Body" type="MeshInstance3D" parent="."]\nmesh = SubResource("m")\n',
                  extra)


def _shape_scene(*shapes: str) -> bytes:
    subs = "".join(f'[sub_resource type="{t}" id="s{i}"]\n\n' for i, t in enumerate(shapes))
    nodes = "".join(f'[node name="C{i}" type="CollisionShape3D" parent="."]\nshape = SubResource("s{i}")\n\n'
                    for i in range(len(shapes)))
    return _scene(HEAD, BOX, subs, ROOT_NODE, MESH_BODY, nodes)


def _invalid(facts: SourceFacts, detail: str, collision: tuple[int, list[str]] | None = None) -> None:
    with pytest.raises(ServiceError) as e:
        _evidence(facts, collision)
    assert e.value.details["detail"] == detail


def test_valid_surface_is_verified(tmp_path: Path) -> None:
    report, facts = _build(tmp_path, {"scenes/main.tscn": _body()})
    assert report.ok, report.errors
    assert _evidence(facts)["source_surfaces"] == {"verified": 1, "node_only": 0, "unverified": 0}
    assert _evidence(facts) == {"source_surfaces": {"verified": 1, "node_only": 0, "unverified": 0},
                                "collision": "absent", "conversion_report": "publisher_declared"}


def test_missing_node(tmp_path: Path) -> None:
    report, facts = _build(tmp_path, {"scenes/main.tscn": _body()}, node="Nope")
    assert report.ok
    _invalid(facts, "source_surface_missing")


def test_surface_index_out_of_range(tmp_path: Path) -> None:
    _, facts = _build(tmp_path, {"scenes/main.tscn": _body()}, surface=1)
    _invalid(facts, "source_surface_index")


def test_surface_on_non_mesh_node(tmp_path: Path) -> None:
    files = {"scenes/main.tscn": _scene(HEAD, ROOT_NODE, '[node name="Body" type="Node3D" parent="."]\n')}
    _, facts = _build(tmp_path, files)
    _invalid(facts, "source_surface_not_mesh")


def test_csg_node_is_node_only(tmp_path: Path) -> None:
    files = {"scenes/main.tscn": _scene(HEAD, ROOT_NODE, '[node name="Body" type="CSGBox3D" parent="."]\n')}
    _, facts = _build(tmp_path, files, caps=[*BASE, "csg_static"])
    assert _evidence(facts)["source_surfaces"]["node_only"] == 1


def test_collision_count_and_type_mismatch(tmp_path: Path) -> None:
    files = {"scenes/main.tscn": _shape_scene("BoxShape3D", "BoxShape3D")}
    report, facts = _build(tmp_path, files, caps=[*BASE, "static_collision"])
    assert report.ok, report.errors
    _invalid(facts, "collision_mismatch", (1, ["box"]))
    _invalid(facts, "collision_mismatch", (2, ["sphere"]))
    assert _evidence(facts, (2, ["box"]))["collision"] == "verified"


def test_nested_scene_reuse_is_accepted_and_doubles_collision(tmp_path: Path) -> None:
    main = _scene(HEAD, BOX, _ext("scenes/part.tscn"), "\n", ROOT_NODE, MESH_BODY,
                  '[node name="A" parent="." instance=ExtResource("1")]\n\n'
                  '[node name="B" parent="." instance=ExtResource("1")]\n')
    files = {"scenes/main.tscn": main, "scenes/part.tscn": _shape_scene("BoxShape3D")}
    report, facts = _build(tmp_path, files, caps=[*BASE, "static_collision"])
    assert report.ok, report.errors
    assert report.structure is not None and report.structure.shapes == ["box", "box"]
    assert {"A/C0", "B/C0"} <= set(report.structure.nodes)
    _invalid(facts, "collision_mismatch", (1, ["box"]))
    assert _evidence(facts, (2, ["box"]))["collision"] == "verified"


def test_scene_instance_cycle(tmp_path: Path) -> None:
    def scene(target: str) -> bytes:
        return _scene(HEAD, _ext(target), "\n", ROOT_NODE, '[node name="X" parent="." instance=ExtResource("1")]\n')

    files = {"scenes/main.tscn": scene("scenes/b.tscn"), "scenes/b.tscn": scene("scenes/main.tscn")}
    report, _ = _build(tmp_path, files)
    assert not report.ok and "instance_cycle" in _details(report)
    assert report.structure is None


def _shader_files(inc_a: str, inc_b: str) -> dict[str, bytes]:
    return {
        "shaders/a.gdshaderinc": f'#include "res://shaders/{inc_a}.gdshaderinc"\n'.encode(),
        "shaders/b.gdshaderinc": f'#include "res://shaders/{inc_b}.gdshaderinc"\n'.encode() if inc_b else b"\n",
        "shaders/s.gdshader": b'shader_type spatial;\n#include "res://shaders/a.gdshaderinc"\n',
        "shaders/t.gdshader": b'shader_type spatial;\n#include "res://shaders/b.gdshaderinc"\n',
        "materials/m.tres": b'[gd_resource type="ShaderMaterial" format=3]\n\n'
                            b'[ext_resource type="Shader" path="res://shaders/s.gdshader" id="1"]\n'
                            b'[ext_resource type="Shader" path="res://shaders/t.gdshader" id="2"]\n\n'
                            b'[resource]\nshader = ExtResource("1")\n',
        "scenes/main.tscn": _scene(HEAD, '[ext_resource type="ShaderMaterial" path="res://materials/m.tres" id="1"]\n\n',
                                   ROOT_NODE),
    }


def test_shader_include_cycle(tmp_path: Path) -> None:
    report, _ = _build(tmp_path, _shader_files("b", "a"), caps=[*BASE, "shader_source"])
    assert not report.ok and "include_cycle" in _details(report)


def test_shared_include_is_not_a_cycle(tmp_path: Path) -> None:
    files = _shader_files("b", "")
    files["shaders/t.gdshader"] = b'shader_type spatial;\n#include "res://shaders/b.gdshaderinc"\n'
    report, _ = _build(tmp_path, files, caps=[*BASE, "shader_source"])
    assert report.ok, report.errors


def test_surface_under_glb_instance_is_unverified(tmp_path: Path) -> None:
    files = {"models/rock.glb": rock_files()["models/rock.glb"], "scenes/main.tscn": _scene(
        HEAD, _ext("models/rock.glb"), "\n", ROOT_NODE, '[node name="Mesh" parent="." instance=ExtResource("1")]\n')}
    report, facts = _build(tmp_path, files, node="Mesh/Deep")
    assert report.ok, report.errors
    assert _evidence(facts)["source_surfaces"]["unverified"] == 1
    assert _evidence(facts, (1, ["box"]))["collision"] == "unverified"
