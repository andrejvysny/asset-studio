"""GodotStaticSourcePackageV1 static validator against every contract fixture, plus extraction safety."""
from __future__ import annotations

import io
import json
import struct
import zipfile
from pathlib import Path
from typing import Any

import pytest
from assetstudio_processing.source_package import SourcePackageReport, validate_source_package

CONTRACT = Path(__file__).resolve().parents[2] / "contracts/godot-integration/v1"
FIXTURES = CONTRACT / "fixtures"
CAPS = json.loads((CONTRACT / "capabilities.json").read_text())
ENTRIES = [f for f in json.loads((FIXTURES / "INDEX.json").read_text())["fixtures"] if f["kind"] == "source_package"]
VALID = [f for f in ENTRIES if f["expected"] == "valid"]
HOSTILE = [f for f in ENTRIES if f["expected"] != "valid"]
VALID_CAPS = {
    "csg_hut": ["godot_text_scene_v1", "csg_static"],
    "custom_shader_crystal": ["godot_text_scene_v1", "shader_source"],
    "vertex_color_rock_with_collision": ["godot_text_scene_v1", "static_collision"],
    "primitive_prop": ["godot_text_scene_v1"],
}


def _run(zip_path: Path, tmp_path: Path, **kw: Any) -> SourcePackageReport:
    staging = tmp_path / "staging"
    return validate_source_package(zip_path, staging, capabilities=CAPS, **kw)


def _fixture(rel: str) -> Path:
    return FIXTURES / rel


def test_index_has_source_packages() -> None:
    assert len(VALID) == 7 and len(HOSTILE) == 18


@pytest.mark.parametrize("entry", VALID, ids=lambda e: Path(e["path"]).stem)
def test_valid_packages_pass(entry: dict[str, Any], tmp_path: Path) -> None:
    report = _run(_fixture(entry["path"]), tmp_path)
    assert report.errors == [] and report.ok
    assert report.manifest is not None and report.manifest_sha256
    assert set(report.files) == {f.path for f in report.manifest.files}
    assert report.dependency_closure and report.expanded_bytes > 0
    expected = VALID_CAPS.get(Path(entry["path"]).stem)
    if expected:
        assert report.detected_capabilities == expected
    shader = "shader_source" in report.detected_capabilities
    assert [w.detail for w in report.warnings] == (["shader_source_desktop_trust"] if shader else [])


def test_prop_cluster_closure_lists_asset_dependency(tmp_path: Path) -> None:
    report = _run(_fixture("source_packages/valid/prop_cluster.zip"), tmp_path)
    assert report.manifest is not None
    assert report.asset_dependencies == sorted(report.manifest.asset_dependencies)
    assert len(report.asset_dependencies) == 1
    assert report.dependency_closure == ["scenes/cluster.tscn"]


def test_crystal_closure_follows_shader_includes(tmp_path: Path) -> None:
    report = _run(_fixture("source_packages/valid/custom_shader_crystal.zip"), tmp_path)
    assert report.dependency_closure == sorted(report.files)


@pytest.mark.parametrize("entry", HOSTILE, ids=lambda e: Path(e["path"]).stem)
def test_hostile_packages_rejected(entry: dict[str, Any], tmp_path: Path) -> None:
    report = _run(_fixture(entry["path"]), tmp_path)
    assert not report.ok and report.errors
    first = report.errors[0]
    assert first.code == entry["expected"], report.errors
    assert first.detail == entry["detail"], report.errors


def test_traversal_member_never_written_outside_staging(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    report = validate_source_package(_fixture("source_packages/hostile/path_traversal.zip"), outer / "staging",
                                     capabilities=CAPS)
    assert report.errors[0].detail == "path_traversal"
    assert sorted(p.name for p in outer.iterdir()) == ["staging"]
    assert list((outer / "staging").iterdir()) == []


def test_symlink_member_not_followed_or_created(tmp_path: Path) -> None:
    report = _run(_fixture("source_packages/hostile/symlink_member.zip"), tmp_path)
    assert report.errors[0].detail == "symlink"
    assert not any(p.is_symlink() for p in (tmp_path / "staging").rglob("*"))
    assert not (tmp_path / "staging/scenes/link.tscn").exists()


def _patch_central_size(raw: bytes, name: str, new_size: int) -> bytes:
    out = bytearray(raw)
    pos = 0
    while (pos := out.find(b"PK\x01\x02", pos)) != -1:
        name_len = struct.unpack_from("<H", out, pos + 28)[0]
        if bytes(out[pos + 46:pos + 46 + name_len]).decode() == name:
            struct.pack_into("<I", out, pos + 24, new_size)
        pos += 4
    return bytes(out)


def test_real_bytes_beyond_declared_size_abort(tmp_path: Path) -> None:
    src = _fixture("source_packages/valid/primitive_prop.zip").read_bytes()
    zip_path = tmp_path / "lying.zip"
    zip_path.write_bytes(_patch_central_size(src, "scenes/prop.tscn", 20))
    report = _run(zip_path, tmp_path)
    assert (report.errors[0].code, report.errors[0].detail) == ("resource_limit", "size_exceeds_declared")


def test_trailing_data_after_end_record_rejected(tmp_path: Path) -> None:
    zip_path = tmp_path / "trail.zip"
    zip_path.write_bytes(_fixture("source_packages/valid/primitive_prop.zip").read_bytes() + b"junk")
    assert _run(zip_path, tmp_path).errors[0].detail == "trailing_data"


def test_resolve_dependency_rejection(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def deny(key: str, dep: dict[str, Any]) -> str | None:
        calls.append((key, dep))
        return "dependency is not available to this caller"

    report = _run(_fixture("source_packages/valid/prop_cluster.zip"), tmp_path, resolve_dependency=deny)
    assert not report.ok
    assert [(e.code, e.detail) for e in report.errors] == [("unsupported_source_dependency", "dependency_unavailable")]
    assert len(calls) == 1 and calls[0][0] == report.errors[0].path and "asset_ref" in calls[0][1]


def test_resolve_dependency_accepts_and_skips_unused(tmp_path: Path) -> None:
    seen: list[str] = []
    report = _run(_fixture("source_packages/valid/prop_cluster.zip"), tmp_path,
                  resolve_dependency=lambda key, dep: seen.append(key))
    assert report.ok and seen == report.asset_dependencies
    plain: list[str] = []
    _run_plain = validate_source_package(_fixture("source_packages/valid/primitive_prop.zip"), tmp_path / "s2",
                                         capabilities=CAPS, resolve_dependency=lambda key, dep: plain.append(key))
    assert _run_plain.ok and plain == []


def test_non_empty_staging_rejected(tmp_path: Path) -> None:
    (tmp_path / "staging").mkdir()
    (tmp_path / "staging/x").write_text("x")
    with pytest.raises(ValueError, match="empty"):
        _run(_fixture("source_packages/valid/primitive_prop.zip"), tmp_path)


def test_not_a_zip(tmp_path: Path) -> None:
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip at all")
    report = _run(junk, tmp_path)
    assert (report.errors[0].code, report.errors[0].detail) == ("unsafe_package", "bad_zip")


def test_missing_manifest(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("scenes/a.tscn", b"[gd_scene format=3]\n")
    path = tmp_path / "nomanifest.zip"
    path.write_bytes(buf.getvalue())
    assert _run(path, tmp_path).errors[0].detail == "manifest_missing"
