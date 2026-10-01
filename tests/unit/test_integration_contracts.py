"""Godot integration contract v1: schemas, typed models, error codes/capabilities, fixture bundle (AS-01)."""
from __future__ import annotations

import hashlib
import importlib
import json
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from assetstudio_core import canonical_v1
from assetstudio_core.delivery import (
    INTEGRATION_ERROR_CODES,
    KNOWN_CAPABILITIES,
    MAX_FILES,
    AssetDescriptorV1,
    AssetRef,
    DeliveryManifestV1,
    descriptor_bytes,
    manifest_bytes,
    parse_descriptor,
    parse_manifest,
    strict_loads,
)
from assetstudio_core.project_lock import lock_bytes, parse_lock
from assetstudio_core.publication_draft import draft_bytes, parse_draft
from assetstudio_core.source_manifest import MANIFEST_NAME, parse_source_manifest, source_manifest_bytes
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts/godot-integration/v1"
FIXTURES = CONTRACT / "fixtures"
INDEX = json.loads((FIXTURES / "INDEX.json").read_text())["fixtures"]

# fixture kind -> (schema file, parser, canonical writer)
KINDS: dict[str, tuple[str, Callable[[bytes], BaseModel], Callable[[Any], bytes]]] = {
    "descriptor": ("asset-descriptor.schema.json", parse_descriptor, descriptor_bytes),
    "manifest": ("delivery-manifest.schema.json", parse_manifest, manifest_bytes),
    "lock": ("project-lock.schema.json", parse_lock, lock_bytes),
    "source_manifest": ("static-source-package.schema.json", parse_source_manifest, source_manifest_bytes),
    "descriptor_draft": ("publication-descriptor-draft.schema.json", parse_draft, draft_bytes),
}
SCHEMA_FILES = sorted(CONTRACT.glob("*.schema.json"))


def _registry() -> Registry:
    resources = []
    for path in SCHEMA_FILES:
        doc = json.loads(path.read_text())
        resources.append((doc["$id"], Resource.from_contents(doc)))
    return Registry().with_resources(resources)


REGISTRY = _registry()


def validator(schema_file: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((CONTRACT / schema_file).read_text()), registry=REGISTRY)


def entries(kind: str, expected: set[str]) -> list[dict[str, Any]]:
    return [e for e in INDEX if e["kind"] == kind and e["expected"] in expected]


def _ids(items: list[dict[str, Any]]) -> list[str]:
    return [e["path"] for e in items]


JSON_CASES = [e for k in KINDS for e in entries(k, {"schema_valid", "schema_invalid", "semantic_invalid"})]


def test_schemas_are_valid_draft_2020_12() -> None:
    assert len(SCHEMA_FILES) == 7
    for path in SCHEMA_FILES:
        Draft202012Validator.check_schema(json.loads(path.read_text()))


@pytest.mark.parametrize("entry", JSON_CASES, ids=_ids(JSON_CASES))
def test_json_fixture_outcome(entry: dict[str, Any]) -> None:
    schema_file, parse, writer = KINDS[entry["kind"]]
    raw = (FIXTURES / entry["path"]).read_bytes()
    try:
        model: BaseModel | None = parse(raw)
    except ValueError:  # pydantic ValidationError, strict_loads and EncodingError all derive from ValueError
        model = None
    schema_ok = validator(schema_file).is_valid(json.loads(raw))
    expected = entry["expected"]
    assert (schema_ok, model is not None) == {
        "schema_valid": (True, True), "schema_invalid": (False, False), "semantic_invalid": (True, False)}[expected]
    if expected == "schema_valid":
        assert writer(model) == raw  # writer stability: stored bytes are exactly the canonical encoding


def test_index_covers_every_file_with_matching_hash() -> None:
    listed = {e["path"] for e in INDEX}
    on_disk = {p.relative_to(FIXTURES).as_posix() for p in FIXTURES.rglob("*") if p.is_file()} - {"INDEX.json"}
    assert listed == on_disk
    for e in INDEX:
        assert hashlib.sha256((FIXTURES / e["path"]).read_bytes()).hexdigest() == e["sha256"]
        assert e["description"]


def test_fixture_bundle_size_budget() -> None:
    sizes = [p.stat().st_size for p in FIXTURES.rglob("*") if p.is_file()]
    assert max(sizes) < 200 * 1024
    assert sum(sizes) < 2 * 1024 * 1024


def test_fixture_matrix_coverage() -> None:
    names = {Path(e["path"]).stem for e in INDEX if e["path"].startswith("source_packages/valid/")}
    assert names >= {"primitive_prop", "textured_tree", "vertex_color_rock_with_collision", "csg_hut",
                     "custom_shader_crystal", "primitive_prop_v2"}
    descs = [parse_descriptor((FIXTURES / e["path"]).read_bytes()) for e in entries("descriptor", {"schema_valid"})]
    assert len({d.asset_ref.library_id for d in descs}) == 2
    assert len({d.asset_ref.version_id for d in descs if d.asset_ref.asset_id.endswith("aa")}) == 2
    assert len({d.asset_ref.key() for d in descs}) == len(descs)


def _zip_members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        return {i.filename: zf.read(i) for i in zf.infolist()}


VALID_ZIPS = entries("source_package", {"valid"})


@pytest.mark.parametrize("entry", VALID_ZIPS, ids=_ids(VALID_ZIPS))
def test_valid_source_package(entry: dict[str, Any]) -> None:
    members = _zip_members(FIXTURES / entry["path"])
    raw = members[MANIFEST_NAME]
    manifest = parse_source_manifest(raw)
    assert source_manifest_bytes(manifest) == raw
    assert validator("static-source-package.schema.json").is_valid(json.loads(raw))
    declared = {f.path: f for f in manifest.files}
    assert set(members) == set(declared) | {MANIFEST_NAME}
    for path, f in declared.items():
        assert hashlib.sha256(members[path]).hexdigest() == f.sha256 and len(members[path]) == f.size
    assert set(manifest.capabilities) <= set(KNOWN_CAPABILITIES)


def test_hostile_packages_are_indexed_with_known_codes() -> None:
    codes = {c["code"] for c in json.loads((CONTRACT / "error-codes.json").read_text())["codes"]}
    hostile = [e for e in INDEX if e["path"].startswith("source_packages/hostile/")]
    assert len(hostile) >= 18
    for e in hostile:
        assert e["expected"] in codes and e.get("detail")
        assert (FIXTURES / e["path"]).is_file()


def test_error_codes_match_python_and_schema() -> None:
    doc = json.loads((CONTRACT / "error-codes.json").read_text())
    assert tuple(c["code"] for c in doc["codes"]) == INTEGRATION_ERROR_CODES
    assert {c["code"] for c in doc["codes"] if c["retryable"]} == {"delivery_preparing", "temporarily_unavailable"}
    schema = json.loads((CONTRACT / "error.schema.json").read_text())
    assert schema["properties"]["error"]["properties"]["code"]["enum"] == list(INTEGRATION_ERROR_CODES)
    ok = {"error": {"code": "stale_pointer", "message": "x", "retryable": False, "details": {}}}
    assert validator("error.schema.json").is_valid(ok)
    assert not validator("error.schema.json").is_valid({"error": {**ok["error"], "code": "nope"}})


def test_capabilities_match_models() -> None:
    caps = json.loads((CONTRACT / "capabilities.json").read_text())
    assert tuple(caps["known_capabilities"]) == KNOWN_CAPABILITIES
    assert (caps["contract_version"], caps["api_version"], caps["source_package_version"]) == (1, 1, 1)
    assert caps["limits"]["source_max_files"] == MAX_FILES
    schema = json.loads((CONTRACT / "asset-ref.schema.json").read_text())
    assert schema["$defs"]["capability"]["enum"] == list(KNOWN_CAPABILITIES)
    rep = schema["$defs"]["representation"]["enum"]
    assert caps["representations"] == rep


def test_vectors_reproduce() -> None:
    vec = json.loads((FIXTURES / "vectors/canonical-v1.json").read_text())
    for k in vec["asset_keys"]:
        ids = (k["server_id"], k["library_id"], k["asset_id"], k["version_id"])
        assert canonical_v1.length_prefixed(*ids).hex() == k["length_prefixed_hex"]
        assert canonical_v1.asset_key(*ids) == k["asset_key"]
    for d in vec["decimals"]:
        assert canonical_v1.decimal_str(float(d["input_repr"]), d["rounding"]) == d["expected"]
    cases = {"empty object": {}, "decimal strings": {"bounds_min": ["-0.5", "0", "-0.25"],
                                                     "bounds_max": ["0.5", "2", "0.25"]}}
    for c in vec["canonical"]:
        if c["name"] in cases:
            raw = canonical_v1.canonical_bytes(cases[c["name"]])
            assert raw.decode() == c["utf8"] and hashlib.sha256(raw).hexdigest() == c["sha256"]


def _script(name: str) -> Any:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        return importlib.import_module(name)
    finally:
        sys.path.remove(str(ROOT / "scripts"))


def test_generators_match_committed_files() -> None:
    fixtures = _script("make_integration_fixtures")
    assert fixtures.build_all() == fixtures.on_disk()
    vectors = _script("make_integration_vectors")
    assert vectors.build() == vectors.OUT.read_bytes()


def test_zip_members_stored_except_bomb() -> None:
    """Fixture bytes must not depend on a zlib build: only the hand-encoded bomb stream is deflated."""
    for path in FIXTURES.rglob("*.zip"):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                expect = zipfile.ZIP_DEFLATED if path.name == "zip_bomb.zip" and info.filename.endswith("bomb.png") \
                    else zipfile.ZIP_STORED
                assert info.compress_type == expect, (path.name, info.filename)


def test_png_fixtures_use_only_stored_deflate_blocks() -> None:
    sources = _script("integration_fixture_sources")
    png = sources.png_bytes("bark")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat = 8, b""
    while pos < len(png):
        size = int.from_bytes(png[pos:pos + 4], "big")
        if png[pos + 4:pos + 8] == b"IDAT":
            idat += png[pos + 8:pos + 8 + size]
        pos += 12 + size
    body, pos, final = idat[2:-4], 0, False  # skip zlib header and adler32
    assert idat[:2] == b"\x78\x01"
    while not final:
        final = bool(body[pos] & 1)
        assert (body[pos] >> 1) & 3 == 0  # BTYPE=00 stored
        pos += 5 + int.from_bytes(body[pos + 1:pos + 3], "little")
    assert pos == len(body)


def _descriptor() -> dict[str, Any]:
    return json.loads((FIXTURES / "descriptors/valid/primitive_prop.json").read_bytes())


def test_asset_ref_key_and_strictness() -> None:
    ref = AssetRef.model_validate(_descriptor()["asset_ref"])
    assert ref.key() == canonical_v1.asset_key(ref.server_id, ref.library_id, ref.asset_id, ref.version_id)
    with pytest.raises(ValidationError):
        AssetRef.model_validate({**_descriptor()["asset_ref"], "server_id": "6F1C2A52-3C2E-4D4B-9A57-0B6F6F0C1D2E"})
    with pytest.raises(ValidationError):
        ref.asset_id = "ast_00000000000000ab"  # frozen


def test_strict_loads_rejects_floats_nan_and_duplicates() -> None:
    for bad in (b'{"a":1.5}', b'{"a":1e3}', b'{"a":NaN}', b'{"a":1,"a":2}'):
        with pytest.raises(ValueError):
            strict_loads(bad)
    assert strict_loads(b'{"a":[1,"1.5",null,true]}') == {"a": [1, "1.5", None, True]}


def test_descriptor_rejects_floats_in_provenance() -> None:
    doc = _descriptor()
    doc["source_provenance"] = {"nested": [{"x": 0.5}]}
    with pytest.raises(ValidationError):
        AssetDescriptorV1.model_validate(doc)


def test_safe_path_depth_limit_matches_schema() -> None:
    schema = validator("delivery-manifest.schema.json")
    manifest = json.loads((FIXTURES / "manifests/valid/portable_primitive_prop.json").read_bytes())
    for depth, ok in ((32, True), (33, False)):
        path = "/".join(["d"] * (depth - 1) + ["f.glb"])
        doc = {**manifest, "entrypoint": path, "files": [{**manifest["files"][0], "path": path}]}
        assert schema.is_valid(doc) is ok
        try:
            DeliveryManifestV1.model_validate(doc)
            assert ok
        except ValidationError:
            assert not ok

