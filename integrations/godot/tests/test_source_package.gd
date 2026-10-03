extends "res://tests/source_test_base.gd"
# Client-side static validator of godot_static_source_v1 archives: every hostile fixture is rejected with the
# code/detail of fixtures/INDEX.json, every valid fixture passes, the policy constants match capabilities.json.

const Package = preload("res://addons/assetstudio/project/as_source_package.gd")
const Policy = preload("res://addons/assetstudio/project/as_srcpkg_policy.gd")
const SourceZip = preload("res://addons/assetstudio/project/as_srcpkg_zip.gd")
const Media = preload("res://addons/assetstudio/project/as_srcpkg_media.gd")

const VALID: PackedStringArray = ["csg_hut", "custom_shader_crystal", "primitive_prop", "primitive_prop_v2",
		"prop_cluster", "textured_tree", "vertex_color_rock_with_collision", "array_mesh_prop"]


func _index_entries(prefix: String) -> Array:
	var parsed: Variant = JSON.parse_string(read_text(contracts_dir().path_join("fixtures/INDEX.json")))
	return (parsed["fixtures"] as Array).filter(func(e: Dictionary) -> bool: return str(e["path"]).begins_with(prefix))


func test_every_hostile_fixture_is_rejected_with_its_index_code_and_detail() -> void:
	var entries: Array = _index_entries("source_packages/hostile/")
	assert_true(entries.size() >= 18, "INDEX lists the hostile packages (%d)" % entries.size())
	for e: Dictionary in entries:
		var r: RefCounted = Package.validate(contracts_dir().path_join("fixtures").path_join(e["path"]))
		assert_true(not r.ok, "%s must be rejected" % e["path"])
		if r.ok:
			continue
		assert_eq(r.code, e["expected"], "code of %s (%s)" % [e["path"], r.message])
		assert_eq(r.details.get("detail"), e["detail"], "detail of %s" % e["path"])


func test_every_valid_fixture_passes() -> void:
	for name: String in VALID:
		var r: RefCounted = Package.validate(zip_file("valid", name))
		assert_true(r.ok, "%s: %s" % [name, r.describe()])
	var valid_entries: Array = _index_entries("source_packages/valid/")
	assert_eq(valid_entries.size(), VALID.size(), "INDEX valid list is covered")


func test_validation_facts() -> void:
	var crystal: RefCounted = Package.validate(zip_file("valid", "custom_shader_crystal"))
	assert_true(crystal.value["shader_source"], "shader package is flagged")
	assert_eq(crystal.value["entry_scene"], "scenes/crystal.tscn", "entry scene")
	assert_true(not Package.validate(zip_file("valid", "csg_hut")).value["shader_source"], "plain package is not flagged")
	var cluster: RefCounted = Package.validate(zip_file("valid", "prop_cluster"))
	assert_eq(cluster.value["dependencies"], ["3e4cecabdb6c9bac29d5f9c655852e10f1d96abdb437cee386bfecf3974b1cbf"], "asset dependency")
	var hut: RefCounted = Package.validate(zip_file("valid", "csg_hut"))
	assert_true((hut.value["detected"] as Array).has("csg_static"), "csg detected from the node types")
	assert_eq(Package.validate(zip_file("valid", "no_such_package")).code, Result.CODE_IO_ERROR, "missing file")


func test_policy_matches_capabilities_json() -> void:
	var caps: Dictionary = JSON.parse_string(read_text(contracts_dir().path_join("capabilities.json")))
	var sp: Dictionary = caps["source_package"]
	assert_eq(Array(Policy.ALLOWED_EXTENSIONS), sp["allowed_extensions"], "allowed_extensions")
	assert_eq(Array(Policy.FORBIDDEN_EXTENSIONS), sp["forbidden_extensions"], "forbidden_extensions")
	assert_eq(Array(Policy.ALLOWED_NODE_TYPES), sp["allowed_node_types"], "allowed_node_types")
	assert_eq(Array(Policy.ALLOWED_RESOURCE_TYPES), sp["allowed_resource_types"], "allowed_resource_types")
	assert_eq(Array(Policy.KNOWN_CAPABILITIES), caps["known_capabilities"], "known_capabilities")
	var lim: Dictionary = caps["limits"]
	assert_eq(Policy.MAX_FILES, int(lim["source_max_files"]), "max files")
	assert_eq(Policy.MAX_DEPTH, int(lim["source_max_depth"]), "max depth")
	assert_eq(Policy.MAX_EXPANDED_BYTES, int(lim["source_expanded_max_bytes"]), "expanded")
	assert_eq(Policy.MAX_UPLOAD_BYTES, int(lim["publication_upload_max_bytes"]), "upload")


## Builds a stored (method 0) zip in memory: [[name, bytes], ...] with a correct central directory.
func _zip_bytes(entries: Array, trailing: PackedByteArray = PackedByteArray()) -> PackedByteArray:
	var out := PackedByteArray()
	var central := PackedByteArray()
	for e: Array in entries:
		var name: PackedByteArray = (e[0] as String).to_utf8_buffer()
		var data: PackedByteArray = e[1]
		var offset: int = out.size()
		var local := PackedByteArray()
		local.resize(30)
		local.encode_u32(0, 0x04034b50)
		local.encode_u32(18, data.size())
		local.encode_u32(22, data.size())
		local.encode_u16(26, name.size())
		out.append_array(local)
		out.append_array(name)
		out.append_array(data)
		var cd := PackedByteArray()
		cd.resize(46)
		cd.encode_u32(0, 0x02014b50)
		cd.encode_u32(20, data.size())
		cd.encode_u32(24, data.size())
		cd.encode_u16(28, name.size())
		cd.encode_u32(38, 0x81A40000)
		cd.encode_u32(42, offset)
		central.append_array(cd)
		central.append_array(name)
	var cd_off: int = out.size()
	out.append_array(central)
	var eocd := PackedByteArray()
	eocd.resize(22)
	eocd.encode_u32(0, 0x06054b50)
	eocd.encode_u16(8, entries.size())
	eocd.encode_u16(10, entries.size())
	eocd.encode_u32(12, central.size())
	eocd.encode_u32(16, cd_off)
	out.append_array(eocd)
	out.append_array(trailing)
	return out


func test_container_edge_cases_on_synthetic_archives() -> void:
	var dir: String = tmp_dir("zips")
	var cases: Array = [
		["trailing", _zip_bytes([["a.tscn", "x".to_utf8_buffer()]], PackedByteArray([1, 2, 3])), "trailing_data"],
		["empty", PackedByteArray([1, 2, 3]), "bad_zip"],
		["no_manifest", _zip_bytes([["a.tscn", "x".to_utf8_buffer()]]), "manifest_missing"],
		["dot_segment", _zip_bytes([["a/./b.tscn", "x".to_utf8_buffer()]]), "invalid_path"],
		["empty_segment", _zip_bytes([["a//b.tscn", "x".to_utf8_buffer()]]), "invalid_path"],
		["non_ascii", _zip_bytes([["café.tscn", "x".to_utf8_buffer()]]), "invalid_path"],
		["drive", _zip_bytes([["C:evil.tscn", "x".to_utf8_buffer()]]), "absolute_path"],
		["control", _zip_bytes([["a\tb.tscn", "x".to_utf8_buffer()]]), "control_character"],
		["too_deep", _zip_bytes([["/".join(PackedStringArray(range(33).map(func(i: int) -> String: return "d"))) + ".tscn", "x".to_utf8_buffer()]]), "depth"],
	]
	for c: Array in cases:
		var path: String = dir.path_join("%s.zip" % c[0])
		Fs.write_atomic(path, c[1])
		var r: RefCounted = Package.validate(path)
		assert_true(not r.ok, "%s must fail" % c[0])
		assert_eq(r.details.get("detail") if not r.ok else "", c[2], "detail of %s" % c[0])
	cleanup()


func test_name_rules() -> void:
	assert_true(SourceZip.validate_name("scenes/a_b-c.d.tscn").ok, "plain safe name")
	assert_eq(SourceZip.validate_name("../a.tscn").details["detail"], "path_traversal", "dotdot")
	assert_eq(SourceZip.validate_name("/a.tscn").details["detail"], "absolute_path", "absolute")
	assert_eq(SourceZip.validate_name("a/b/").details["detail"], "invalid_path", "trailing slash is an empty segment")


func test_extension_policy() -> void:
	assert_eq(Package.extension_problem("scenes/a.tscn"), [], "allowed")
	assert_eq(Package.extension_problem("a/project.godot")[0], "forbidden_file", "project.godot anywhere")
	assert_eq(Package.extension_problem("a.SCN")[0], "binary_resource", "case-insensitive forbidden")
	assert_eq(Package.extension_problem("a.PNG")[0], "extension_not_allowed", "allowed list is case-sensitive like the server")
	assert_eq(Package.extension_problem("a.import")[0], "extension_not_allowed", ".import is not allowed")
	assert_eq(Package.extension_problem("noext")[0], "extension_not_allowed", "no extension")


func test_image_and_glb_header_checks() -> void:
	var png := PackedByteArray([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52, 0, 0, 0, 4, 0, 0, 0, 4])
	assert_true(Media.check_image("a.png", png, ".png").ok, "valid png header")
	assert_eq(Media.check_image("a.png", PackedByteArray([1, 2, 3]), ".png").details["detail"], "image_magic_mismatch", "not a png")
	var huge: PackedByteArray = png.duplicate()
	huge.encode_u32(16, 0)
	huge[16] = 0x00
	huge[17] = 0x01
	huge[18] = 0x00
	huge[19] = 0x00
	assert_eq(Media.check_image("a.png", huge, ".png").code, "resource_limit", "65536 px side is over the budget")
	assert_eq(Media.check_glb("a.glb", PackedByteArray([1, 2, 3])).details["detail"], "glb_invalid", "not a glb")
	var rock: PackedByteArray = fixture_bytes_in_zip("vertex_color_rock_with_collision", "models/rock.glb")
	assert_true(Media.check_glb("models/rock.glb", rock).ok, "valid GLB: %s" % Media.check_glb("models/rock.glb", rock).describe())


func fixture_bytes_in_zip(name: String, member: String) -> PackedByteArray:
	var z := ZIPReader.new()
	z.open(zip_file("valid", name))
	var data: PackedByteArray = z.read_file(member)
	z.close()
	return data
