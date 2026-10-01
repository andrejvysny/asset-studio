extends "res://tests/test_case.gd"

const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")
const VECTORS_REL: String = "fixtures/vectors/canonical-v1.json"


func _vectors() -> Dictionary:
	var dir: String = OS.get_environment("ASSETSTUDIO_CONTRACTS_DIR")
	if dir == "":
		dir = ProjectSettings.globalize_path("res://").path_join("../../contracts/godot-integration/v1").simplify_path()
	var path: String = dir.path_join(VECTORS_REL)
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(path))
	if not parsed is Dictionary:
		fail("cannot load vectors from %s" % path)
		return {}
	return parsed


func test_asset_keys() -> void:
	var entries: Array = _vectors().get("asset_keys", [])
	assert_true(not entries.is_empty(), "no asset_keys vectors")
	for e: Dictionary in entries:
		var parts := PackedStringArray([e["server_id"], e["library_id"], e["asset_id"], e["version_id"]])
		assert_eq(Canonical.length_prefixed(parts).hex_encode(), e["length_prefixed_hex"], "length_prefixed %s" % e["name"])
		assert_eq(Canonical.asset_key(e["server_id"], e["library_id"], e["asset_id"], e["version_id"]), e["asset_key"], "asset_key %s" % e["name"])


func test_canonical_sha256_raw_bytes() -> void:
	var entries: Array = _vectors().get("canonical", [])
	assert_true(not entries.is_empty(), "no canonical vectors")
	for e: Dictionary in entries:
		var utf8: String = e["utf8"]
		assert_eq(Canonical.sha256_hex(utf8.to_utf8_buffer()), e["sha256"], "sha256 %s" % e["name"])


func test_decimal_expected_parse() -> void:
	var entries: Array = _vectors().get("decimals", [])
	assert_true(not entries.is_empty(), "no decimals vectors")
	for e: Dictionary in entries:
		var expected: String = e["expected"]
		var r: Dictionary = Canonical.parse_decimal(expected)
		assert_true(r["ok"], "parse_decimal(%s): %s" % [expected, r["error"]])
		assert_true(is_equal_approx(float(r["value"]), expected.to_float()), "value %s" % expected)


func test_decimal_rejects_non_canonical() -> void:
	for bad: String in ["-0", "1.50", "01", "1e3", "+1", ".5", "1.", "1.1234567", "", "1\n"]:
		assert_true(not Canonical.is_canonical_decimal(bad), "should reject %s" % bad.c_escape())
	for good: String in ["0", "-1", "0.000001", "123.456"]:
		assert_true(Canonical.is_canonical_decimal(good), "should accept %s" % good)
