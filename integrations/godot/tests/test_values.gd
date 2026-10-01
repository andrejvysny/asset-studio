extends "res://tests/test_case.gd"

const Descriptor = preload("res://addons/assetstudio/core/as_asset_descriptor.gd")
const Manifest = preload("res://addons/assetstudio/core/as_delivery_manifest.gd")
const AssetRef = preload("res://addons/assetstudio/core/as_asset_ref.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")
const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")


func _contracts_dir() -> String:
	var dir: String = OS.get_environment("ASSETSTUDIO_CONTRACTS_DIR")
	if dir == "":
		dir = ProjectSettings.globalize_path("res://").path_join("../../contracts/godot-integration/v1").simplify_path()
	return dir


func _index() -> Array:
	var path: String = _contracts_dir().path_join("fixtures/INDEX.json")
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(path))
	if not parsed is Dictionary:
		fail("cannot load fixture INDEX at %s" % path)
		return []
	return (parsed as Dictionary)["fixtures"]


func _check_kind(kind: String, parse: Callable) -> int:
	var seen: int = 0
	for e: Dictionary in _index():
		if e["kind"] != kind:
			continue
		var raw: PackedByteArray = FileAccess.get_file_as_bytes(_contracts_dir().path_join("fixtures").path_join(e["path"]))
		assert_eq(Canonical.sha256_hex(raw), e["sha256"], "INDEX sha %s" % e["path"])
		var r: RefCounted = parse.call(raw)
		var valid: bool = e["expected"] == "schema_valid"
		assert_eq(r.ok, valid, "%s expected %s: %s" % [e["path"], e["expected"], r.describe()])
		if r.ok:
			assert_eq(r.value.raw_sha256, e["sha256"], "raw sha %s" % e["path"])
		seen += 1
	return seen


func test_descriptor_fixtures_match_index() -> void:
	assert_true(_check_kind("descriptor", Descriptor.parse_bytes) >= 15, "descriptor fixture count")


func test_manifest_fixtures_match_index() -> void:
	assert_true(_check_kind("manifest", Manifest.parse_bytes) >= 10, "manifest fixture count")


func test_descriptor_rejects_axis_units_and_unknown_fields() -> void:
	var base: Dictionary = _valid_descriptor()
	for mut: Array in [["forward_axis", "-Z"], ["up_axis", "+Z"], ["units", "cm"], ["extra", 1], ["schema_version", 2]]:
		var d: Dictionary = base.duplicate(true)
		d[mut[0]] = mut[1]
		assert_true(not Descriptor.parse_bytes(JSON.stringify(d).to_utf8_buffer()).ok, "must reject %s" % mut[0])
	assert_true(Descriptor.parse_bytes(JSON.stringify(base).to_utf8_buffer()).ok, "base must parse")


func test_parsers_reject_malformed_bytes() -> void:
	assert_true(not Descriptor.parse_bytes(PackedByteArray()).ok, "empty")
	assert_true(not Descriptor.parse_bytes("[]".to_utf8_buffer()).ok, "array")
	assert_true(not Manifest.parse_bytes("{".to_utf8_buffer()).ok, "truncated")
	assert_true(not Manifest.parse_bytes("﻿{}".to_utf8_buffer()).ok, "BOM")


func test_hash_is_over_raw_bytes_not_reserialized() -> void:
	var raw: PackedByteArray = JSON.stringify(_valid_descriptor(), "  ").to_utf8_buffer()
	var r: RefCounted = Descriptor.parse_bytes(raw)
	assert_true(r.ok, "pretty-printed descriptor parses")
	assert_eq(r.value.raw_sha256, Canonical.sha256_hex(raw), "sha over supplied bytes")


func test_safe_paths() -> void:
	for good: String in ["a.glb", "dir/a.glb", "a-b_c.1/x", "..."]:
		assert_eq(Schema.check_safe_path(good, "p"), "", "accept %s" % good)
	for bad: String in ["", "/abs", "../x", "a/../b", "a//b", "a\\b", "./a", "a/.", "a/", "a b", "é", "a:b"]:
		assert_true(Schema.check_safe_path(bad, "p") != "", "reject %s" % bad.c_escape())
	assert_true(Schema.check_safe_path("a/".repeat(32) + "a", "p") != "", "reject depth 33")
	assert_true(Schema.check_safe_path("a".repeat(256), "p") != "", "reject length 256")


func test_manifest_semantic_rules() -> void:
	var base: Dictionary = _valid_manifest()
	assert_true(Manifest.parse_bytes(JSON.stringify(base).to_utf8_buffer()).ok, "base manifest parses")
	var casefold: Dictionary = base.duplicate(true)
	var extra: Dictionary = (casefold["files"][0] as Dictionary).duplicate()
	extra["path"] = "PORTABLE.glb"
	casefold["files"].append(extra)
	assert_true(not Manifest.parse_bytes(JSON.stringify(casefold).to_utf8_buffer()).ok, "case-fold duplicate")
	var url: Dictionary = base.duplicate(true)
	url["files"][0]["url"] = "https://example.invalid/x"
	assert_true(not Manifest.parse_bytes(JSON.stringify(url).to_utf8_buffer()).ok, "url field")
	var big: Dictionary = base.duplicate(true)
	big["files"][0]["size"] = 1073741825
	assert_true(not Manifest.parse_bytes(JSON.stringify(big).to_utf8_buffer()).ok, "size over limit")


func test_asset_ref_parse_and_key() -> void:
	var d: Dictionary = _valid_descriptor()["asset_ref"]
	var r: RefCounted = AssetRef.parse(d)
	assert_true(r.ok, "ref parses")
	assert_eq(r.value.key(), Canonical.asset_key(d["server_id"], d["library_id"], d["asset_id"], d["version_id"]), "key")
	var bad: Dictionary = d.duplicate()
	bad["asset_id"] = "latest"
	assert_true(not AssetRef.parse(bad).ok, "reject non-id")
	bad = d.duplicate()
	bad["extra"] = "x"
	assert_true(not AssetRef.parse(bad).ok, "reject unknown field")


func _fixture_json(rel: String) -> Dictionary:
	return JSON.parse_string(FileAccess.get_file_as_string(_contracts_dir().path_join("fixtures").path_join(rel)))


func _valid_descriptor() -> Dictionary:
	return _fixture_json("descriptors/valid/primitive_prop.json")


func _valid_manifest() -> Dictionary:
	return _fixture_json("manifests/valid/portable_primitive_prop.json")
