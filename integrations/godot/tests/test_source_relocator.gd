extends "res://tests/source_test_base.gd"
# Text-format parser/serializer and relocator of godot_static_source_v1 packages (no regex over arbitrary text).

const GText = preload("res://addons/assetstudio/project/as_godot_text.gd")
const Relocator = preload("res://addons/assetstudio/project/as_source_relocator.gd")

const NEW_MAT: String = "res://assets/library/k/m/source/materials/mat.tres"


func _map() -> Dictionary:
	return {"res://materials/mat.tres": NEW_MAT, "res://shaders/common.gdshaderinc": "res://assets/library/k/m/source/shaders/common.gdshaderinc"}


func test_header_uid_and_ext_resource_path_are_rewritten() -> void:
	var text: String = "[gd_scene load_steps=2 format=3 uid=\"uid://abc\"]\n\n[ext_resource type=\"Material\" uid=\"uid://def\" path=\"res://materials/mat.tres\" id=\"1_m\"]\n\n[node name=\"A\" type=\"Node3D\"]\n"
	var r: RefCounted = Relocator.rewrite_text(text, _map())
	assert_true(r.ok, r.describe())
	assert_eq(r.value["text"], "[gd_scene load_steps=2 format=3]\n\n[ext_resource type=\"Material\" path=\"%s\" id=\"1_m\"]\n\n[node name=\"A\" type=\"Node3D\"]\n" % NEW_MAT, "rewritten text")
	assert_true(r.value["changed"], "changed")


func test_untouched_text_is_returned_byte_identical() -> void:
	var text: String = "[gd_resource type=\"StandardMaterial3D\" format=3]\n\n[resource]\n; a comment\nroughness = 0.7\nname = \"multi\nline = value\"\narr = [1, 2,\n 3]\n"
	var r: RefCounted = Relocator.rewrite_text(text, _map())
	assert_true(r.ok and not r.value["changed"] and r.value["text"] == text, "no uid, no ext_resource: unchanged")


func test_unmapped_reference_is_an_error() -> void:
	var text: String = "[gd_scene format=3]\n\n[ext_resource type=\"Material\" path=\"res://other.tres\" id=\"1\"]\n"
	var r: RefCounted = Relocator.rewrite_text(text, _map())
	assert_true(not r.ok and r.code == "unsupported_source_dependency", "unmapped path refused: %s" % r.describe())
	var no_path: RefCounted = Relocator.rewrite_text("[gd_scene format=3]\n\n[ext_resource type=\"Material\" uid=\"uid://x\" id=\"1\"]\n", _map())
	assert_true(not no_path.ok, "uid-only references are never resolved")


func test_only_ext_resource_paths_change() -> void:
	var text: String = "[gd_scene format=3]\n\n[node name=\"N\" type=\"Node3D\"]\nmetadata/note = \"path=\\\"res://materials/mat.tres\\\" uid=\\\"uid://abc\\\"\"\n"
	var r: RefCounted = Relocator.rewrite_text(text, _map())
	assert_true(r.ok and not r.value["changed"], "a string property that looks like a reference is data, not a reference")


func test_crlf_line_endings_survive_a_rewritten_header() -> void:
	var text: String = "[gd_scene format=3 uid=\"uid://abc\"]\r\n\r\n[node name=\"A\" type=\"Node3D\"]\r\n"
	var r: RefCounted = Relocator.rewrite_text(text, _map())
	assert_eq(r.value["text"], "[gd_scene format=3]\r\n\r\n[node name=\"A\" type=\"Node3D\"]\r\n", "CRLF kept")


func test_inline_shader_code_includes_are_rewritten() -> void:
	var text: String = "[gd_resource type=\"Shader\" format=3]\n\n[resource]\ncode = \"shader_type spatial;\n#include \\\"res://shaders/common.gdshaderinc\\\"\nvoid fragment() {}\n\"\n"
	var r: RefCounted = Relocator.rewrite_text(text, _map())
	assert_true(r.ok, r.describe())
	var again: RefCounted = GText.parse_text(r.value["text"])
	assert_true(again.ok, "result still parses")
	var code: String = ""
	for s: Dictionary in again.value["statements"]:
		if s["kind"] == "prop" and s["key"] == "code":
			code = s["value"]["v"]
	assert_true(code.contains("#include \"res://assets/library/k/m/source/shaders/common.gdshaderinc\""), "include remapped inside the string: %s" % code)
	assert_true(code.begins_with("shader_type spatial;\n") and code.ends_with("void fragment() {}\n"), "rest of the code intact")


func test_shader_include_lines() -> void:
	var src: String = "shader_type spatial;\n#include \"res://shaders/common.gdshaderinc\"\n  #  include \"rel.gdshaderinc\" // keep\n#include_x \"zzz\"\nuniform float a; // #include \"res://nope\"\n"
	var r: RefCounted = Relocator.rewrite_shader(src, _map())
	assert_true(r.ok, r.describe())
	assert_eq(r.value["text"], "shader_type spatial;\n#include \"res://assets/library/k/m/source/shaders/common.gdshaderinc\"\n  #  include \"rel.gdshaderinc\" // keep\n#include_x \"zzz\"\nuniform float a; // #include \"res://nope\"\n", "only real include directives of res:// paths change")
	var bad: RefCounted = Relocator.rewrite_shader("#include \"res://missing.gdshaderinc\"\n", _map())
	assert_true(not bad.ok and bad.code == "unsupported_source_dependency", "unmapped include refused")
	assert_true(not Relocator.rewrite_shader("#include <x>\n", _map()).ok, "malformed include refused")
	assert_true(not Relocator.rewrite_shader("#include \"a\" garbage\n", _map()).ok, "garbage after the include refused")


func test_include_line_parser() -> void:
	assert_eq(GText.parse_include("int x;"), {}, "not an include")
	var inc: Dictionary = GText.parse_include("\t#include \"a/b.gdshaderinc\"  // c")
	assert_true(inc["ok"] and inc["arg"] == "a/b.gdshaderinc", "arg")
	assert_eq(inc["head"] + GText.quote(inc["arg"]) + inc["tail"], "\t#include \"a/b.gdshaderinc\"  // c", "rebuilds the line")
	assert_eq(GText.parse_include("#include"), {"ok": false}, "bare include is malformed")


func _first_prop_value(text: String) -> Dictionary:
	var parsed: RefCounted = GText.parse_text("[gd_resource type=\"X\" format=3]\n\n[resource]\n" + text)
	assert_true(parsed.ok, "parse: %s" % parsed.describe())
	if not parsed.ok:
		return {}
	for s: Dictionary in parsed.value["statements"]:
		if s["kind"] == "prop":
			return s["value"]
	return {}


func test_value_forms() -> void:
	assert_eq(_first_prop_value("a = -0.5e-3\n"), {"t": "num", "v": "-0.5e-3"}, "negative exponent")
	assert_eq(_first_prop_value("a = 0x1F\n"), {"t": "num", "v": "0x1F"}, "hex")
	assert_eq(_first_prop_value("a = -inf\n"), {"t": "num", "v": "-inf"}, "special float")
	assert_eq(_first_prop_value("a = true\n"), {"t": "lit", "v": "true"}, "bool")
	assert_eq(_first_prop_value("a = &\"n\"\n")["name"], "StringName", "string name")
	assert_eq(_first_prop_value("a = ExtResource(\"1_a\")\n"), {"t": "ref", "kind": "ExtResource", "id": "1_a"}, "ref")
	assert_eq(_first_prop_value("a = SubResource(7)\n")["id"], "7", "numeric ref id")
	assert_eq(_first_prop_value("a = Array[int]([1, 2])\n")["name"], "Array[int]", "typed array")
	assert_eq(_first_prop_value("a = {\"k\": [1, Vector3(1, 2, 3)], 2: null}\n")["items"].size(), 2, "dictionary")
	assert_eq(_first_prop_value("a = \"x\\\"y\\n\\u00e9\"\n")["v"], "x\"y\n\u00e9", "escapes")
	assert_eq(_first_prop_value("a = PackedFloat32Array(0, 1,\n 2)\n")["args"].size(), 3, "multi-line call")


func test_parse_failures() -> void:
	var header: String = "[gd_resource type=\"X\" format=3]\n\n[resource]\n"
	for bad: String in ["a = 1a\n", "a = \"open\n", "a = [1, 2\n", "a = (1)\n", "a = Foo\n", "a = [1,]\n", "a = {[1]: 2}\n",
			"a = 1 2\n", "a = \"\\u00\"\n", "= 3\n", "a = ExtResource(1, 2)\n", "a = \"nul\\0\"\n"]:
		assert_true(not GText.parse_text(header + bad).ok, "must fail: %s" % bad.strip_edges())
	assert_eq(GText.parse_text("[gd_scene format=2]\n").details["detail"], "unsupported_format", "format 3 only")
	assert_eq(GText.parse_text("[node name=\"a\"]\n").details["detail"], "parse_error", "must start with a root header")
	assert_eq(GText.parse_text("[gd_scene format=3]\nx = 1\n").details["detail"], "parse_error", "no properties after the root header")
	assert_eq(GText.parse_bytes(PackedByteArray([0x52, 0x53, 0x52, 0x43, 0, 0])).details["detail"], "binary_resource", "binary magic")
	assert_eq(GText.parse_bytes(PackedByteArray([0x5b, 0xff, 0xfe])).details["detail"], "not_utf8", "not utf-8")
	var deep: String = header + "a = " + "[".repeat(70) + "]".repeat(70) + "\n"
	assert_eq(GText.parse_text(deep).code, "resource_limit", "nesting is bounded")
	assert_true(GText.parse_text("\ufeff[gd_scene format=3]\n").ok, "BOM is tolerated")


func test_text_format_4_with_base64_packed_array_parses_and_other_formats_are_refused() -> void:
	var scene: String = "[gd_scene format=4]\n\n[sub_resource type=\"ArrayMesh\" id=\"A_1\"]\n_surfaces = [{\n\"aabb\": AABB(-0.5, 1e-05, 0, 1, 1, 1),\n\"vertex_data\": PackedByteArray(\"AAAAvwAAAD8=\")\n}]\n"
	assert_true(GText.parse_text(scene).ok, "format=4 with a base64 PackedByteArray parses")
	assert_true(GText.parse_text("[gd_scene format=3]\n").ok, "format=3 still parses")
	for fmt: String in ["5", "2"]:
		assert_eq(GText.parse_text("[gd_scene format=%s]\n" % fmt).details["detail"], "unsupported_format", "format=%s refused" % fmt)


func test_every_valid_text_member_roundtrips_with_an_identity_map() -> void:
	for name: String in ["csg_hut", "custom_shader_crystal", "primitive_prop", "primitive_prop_v2", "prop_cluster", "textured_tree", "vertex_color_rock_with_collision", "array_mesh_prop"]:
		var z := ZIPReader.new()
		z.open(zip_file("valid", name))
		var manifest: Dictionary = JSON.parse_string(z.read_file("source_manifest.json").get_string_from_utf8())
		var identity: Dictionary = {}
		for ref: String in manifest["resource_map"]:
			identity[ref] = ref
		for f: Dictionary in manifest["files"]:
			var ext: String = str(f["path"]).get_extension()
			if ext != "tscn" and ext != "tres":
				continue
			var text: String = z.read_file(f["path"]).get_string_from_utf8()
			var r: RefCounted = Relocator.rewrite_text(text, identity)
			assert_true(r.ok, "%s/%s: %s" % [name, f["path"], r.describe()])
			var stripped: String = text
			for ref: String in manifest["resource_map"]:
				var uid: Variant = manifest["resource_map"][ref]["original_uid"]
				if uid != null:
					stripped = stripped.replace(" uid=\"%s\"" % uid, "")
			var header_end: int = stripped.find("\n")
			var head: String = stripped.substr(0, header_end)
			var uid_at: int = head.find(" uid=\"")
			if uid_at >= 0:
				head = head.substr(0, uid_at) + head.substr(head.find("\"", uid_at + 6) + 1)
			assert_eq(r.value["text"], head + stripped.substr(header_end), "%s/%s: only uid attributes differ" % [name, f["path"]])
		z.close()
