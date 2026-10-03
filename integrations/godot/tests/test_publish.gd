extends "res://tests/project_test_base.gd"
# AS-09 publisher units: policy mirror, decimals/slugs, multipart bodies, deterministic zip, text scan, GLB
# inspection, journal, and the collector + portable export on small scenes (tests/data/publish; no import needed).

const Policy = preload("res://addons/assetstudio/project/as_srcpkg_policy.gd")
const TextScan = preload("res://addons/assetstudio/project/as_source_text.gd")
const Graph = preload("res://addons/assetstudio/project/as_source_graph.gd")
const GlbInspect = preload("res://addons/assetstudio/project/as_glb_inspect.gd")
const Collector = preload("res://addons/assetstudio/project/as_source_collector.gd")
const Export = preload("res://addons/assetstudio/project/as_portable_export.gd")
const Writer = preload("res://addons/assetstudio/project/as_source_writer.gd")
const Journal = preload("res://addons/assetstudio/project/as_publish_journal.gd")
const Multipart = preload("res://addons/assetstudio/core/as_multipart.gd")
const Dialog = preload("res://addons/assetstudio/editor/as_publish_dialog.gd")
const GlbBuilder = preload("res://tests/glb_builder.gd")

const DATA: String = "res://tests/data/publish/"


func _caps() -> Dictionary:
	return JSON.parse_string(FileAccess.get_file_as_string(contracts_dir().path_join("capabilities.json")))


func test_policy_mirrors_capabilities_json() -> void:
	var caps: Dictionary = _caps()
	var sp: Dictionary = caps["source_package"]
	assert_eq(Array(Policy.ALLOWED_NODE_TYPES), sp["allowed_node_types"], "node types")
	assert_eq(Array(Policy.ALLOWED_RESOURCE_TYPES), sp["allowed_resource_types"], "resource types")
	assert_eq(Array(Policy.ALLOWED_EXTENSIONS), sp["allowed_extensions"], "extensions")
	var lim: Dictionary = caps["limits"]
	assert_eq(Policy.MAX_FILES, int(lim["source_max_files"]), "files")
	assert_eq(Policy.MAX_DEPTH, int(lim["source_max_depth"]), "depth")
	assert_eq(Policy.MAX_EXPANDED_BYTES, int(lim["source_expanded_max_bytes"]), "expanded")
	assert_eq(Policy.MAX_UPLOAD_BYTES, int(lim["publication_upload_max_bytes"]), "upload")
	assert_eq(Policy.IPAD_MAX_TRIANGLES, int(lim["ipad_max_triangles"]), "triangles")
	assert_eq(Policy.IPAD_MAX_NODES, int(lim["ipad_max_nodes"]), "nodes")
	assert_eq(Policy.IPAD_MAX_MATERIALS, int(lim["ipad_max_materials"]), "materials")
	assert_eq(Policy.IPAD_MAX_TEXTURE_PX, int(lim["ipad_max_texture_px"]), "texture px")


func test_decimal_and_slug_helpers() -> void:
	for c: Array in [[0.25, "0.25"], [-0.5, "-0.5"], [1.0, "1"], [100.0, "100"], [-0.0, "0"], [0.000001, "0"], [2.34567891, "2.34568"], [-0.000004, "0"]]:
		assert_eq(Graph.decimal(c[0]), c[1], str(c[0]))
	assert_eq(Graph.slugify("Bark Material!", "x"), "bark_material_", "slug")
	assert_eq(Graph.slugify("__x", "x"), "x", "leading separators trimmed")
	assert_eq(Graph.slugify("", "fallback"), "fallback", "fallback")
	assert_true(RegEx.create_from_string("^[a-z0-9][a-z0-9_.-]{0,63}$").search(Graph.slugify("A" + "b".repeat(100), "x")) != null, "slug pattern")


func test_multipart_body_bounded_and_parsable() -> void:
	var dir: String = tmp_dir("mp")
	write_text(dir.path_join("a.bin"), "FILEDATA")
	var parts: Dictionary = {"source": {"path": dir.path_join("a.bin"), "filename": "source.zip", "media_type": "application/zip"},
			"descriptor": {"bytes": "{}".to_utf8_buffer(), "filename": "descriptor.json", "media_type": "application/json"}}
	var r: RefCounted = Multipart.build(parts, 4096)
	assert_true(r.ok, "build")
	var body: String = (r.value["body"] as PackedByteArray).get_string_from_utf8()
	var boundary: String = str(r.value["content_type"]).get_slice("boundary=", 1)
	assert_true(body.contains("name=\"source\"; filename=\"source.zip\"") and body.contains("FILEDATA") and body.ends_with("--%s--\r\n" % boundary), "layout")
	assert_eq(Multipart.build(parts, 100).code, "resource_limit", "over the limit is refused before reading")
	assert_eq(Multipart.build({"Bad Name": {"bytes": PackedByteArray()}}, 4096).code, "invalid_request", "part names are validated")
	assert_eq(Multipart.build({"source": {"path": dir.path_join("missing")}}, 4096).code, "io_error", "missing file")
	cleanup()


func _members() -> Array:
	return [{"name": "scenes/b.tscn", "data": "[gd_scene format=3]\n".to_utf8_buffer()},
			{"name": "a.png", "data": PackedByteArray([1, 2, 3])},
			{"name": "scenes/a.tres", "data": ("x".repeat(500)).to_utf8_buffer()},
			{"name": "empty.glb", "data": PackedByteArray()}]


func test_zip_writer_is_deterministic_and_readable() -> void:
	var dir: String = tmp_dir("zip")
	var a: RefCounted = Writer.write_zip(_members(), dir.path_join("a.zip"))
	var b: RefCounted = Writer.write_zip(_members(), dir.path_join("b.zip"))
	assert_true(a.ok and b.ok, "write")
	assert_eq(a.value["sha256"], b.value["sha256"], "same members, same bytes")
	var z := ZIPReader.new()
	assert_eq(z.open(dir.path_join("a.zip")), OK, "ZIPReader opens it")
	var names: PackedStringArray = z.get_files()
	assert_eq(Array(names), ["a.png", "empty.glb", "scenes/a.tres", "scenes/b.tscn"], "sorted names")
	assert_eq(z.read_file("scenes/a.tres"), ("x".repeat(500)).to_utf8_buffer(), "deflated member round trips")
	assert_eq(z.read_file("a.png"), PackedByteArray([1, 2, 3]), "stored member round trips")
	assert_eq(z.read_file("empty.glb").size(), 0, "empty member")
	z.close()
	var bytes: PackedByteArray = FileAccess.get_file_as_bytes(dir.path_join("a.zip"))
	assert_eq(bytes.decode_u16(10), 0, "fixed DOS time")
	assert_eq(bytes.decode_u16(12), 0x21, "fixed DOS date (1980-01-01)")
	cleanup()


func test_package_member_grammar_checks() -> void:
	var ok: Array = [{"path": "scenes/a.tscn", "sha256": "", "size": 5, "media_type": "text/x-godot-scene"}]
	assert_true(Writer.check_members(ok, "scenes/a.tscn").ok, "valid")
	assert_eq(Writer.check_members(ok, "scenes/other.tscn").code, "unsafe_package", "entry scene must be a member")
	var dup: Array = ok + [{"path": "Scenes/A.tscn", "sha256": "", "size": 5, "media_type": "x/y"}]
	assert_eq(Writer.check_members(dup, "scenes/a.tscn").code, "unsafe_package", "case-fold duplicate")
	for bad: String in ["scenes/../x.tscn", "/abs.tscn", "a b.tscn", "x.gd", "x.scn", "source_manifest.json", "a\\b.tscn"]:
		assert_eq(Writer.check_members([{"path": bad, "sha256": "", "size": 1, "media_type": "x/y"}], bad).code, "unsafe_package", bad)


func test_text_scan_blocks_unsupported_content() -> void:
	var scene: String = "[gd_scene load_steps=3 format=3]\n\n[ext_resource type=\"Script\" path=\"res://a.gd\" id=\"1\"]\n\n" \
			+ "[node name=\"R\" type=\"Camera3D\"]\nscript = ExtResource(\"1\")\n\n[node name=\"X\" type=\"Mine\" parent=\".\"]\n\n" \
			+ "[connection signal=\"x\" from=\".\" to=\".\" method=\"y\"]\n"
	var text: String = "\n".join(TextScan.problems(TextScan.scan_text(scene), true, "s.tscn"))
	for needle: String in ["script resources are not supported", "Camera3D", "scripts are not supported (property 'script')", "Mine", "signal connections"]:
		assert_true(text.contains(needle), "reports: %s" % needle)
	var good: String = FileAccess.get_file_as_string(DATA + "prop.tscn")
	assert_true(TextScan.problems(TextScan.scan_text(good), true, "prop.tscn").is_empty(), "the prop scene is clean")
	assert_true(not TextScan.problems(TextScan.scan_text("[gd_resource type=\"Mine\" format=3]\n"), false, "m.tres").is_empty(), "custom resource type")
	assert_eq(TextScan.ext_resources(TextScan.scan_text(good))[0]["path"], DATA + "prop_mat.tres", "ext_resource path")
	var broken: Array = TextScan.problems(TextScan.scan_text("[gd_scene format=3]\n[node name=\"A\" type=\"Node3D\"\n"), true, "b.tscn")
	assert_true(broken.size() == 1 and str(broken[0]).begins_with("b.tscn: "), "a parse failure is reported once with the label")
	var inc: Dictionary = TextScan.includes("shader_type spatial;\n#include \"res://x/a.gdshaderinc\"\n  #  include \"b.gdshaderinc\"\n")
	assert_eq(Array(inc["paths"]), ["res://x/a.gdshaderinc"], "res include")
	assert_eq((inc["bad"] as PackedStringArray).size(), 1, "relative include flagged")


func test_text_scan_accepts_engine_material_save_class_for_external_resources_only() -> void:
	var ext: String = "[gd_scene format=3]\n\n[ext_resource type=\"Material\" path=\"res://m.tres\" id=\"1\"]\n\n[node name=\"R\" type=\"Node3D\"]\n"
	assert_true(TextScan.problems(TextScan.scan_text(ext), true, "a.tscn").is_empty(), "Godot writes type=Material for an external material")
	var sub: String = "[gd_scene format=3]\n\n[sub_resource type=\"Material\" id=\"1\"]\n\n[node name=\"R\" type=\"Node3D\"]\n"
	assert_true(not TextScan.problems(TextScan.scan_text(sub), true, "b.tscn").is_empty(), "an inline abstract Material is still refused")


func test_text_scan_accepts_text_format_4_and_refuses_other_formats() -> void:
	var body: String = "\n[node name=\"R\" type=\"Node3D\"]\n"
	assert_true(TextScan.problems(TextScan.scan_text("[gd_scene format=4]\n" + body), true, "a.tscn").is_empty(), "format=4 scene")
	assert_true(TextScan.problems(TextScan.scan_text("[gd_scene format=3]\n" + body), true, "a.tscn").is_empty(), "format=3 scene")
	for fmt: String in ["5", "2"]:
		var problems: String = "\n".join(TextScan.problems(TextScan.scan_text("[gd_scene format=%s]\n" % fmt + body), true, "a.tscn"))
		assert_true(problems.contains("text format 3 or 4"), "format=%s refused" % fmt)


func test_glb_inspect_accepts_static_and_rejects_external_uri() -> void:
	var glb: PackedByteArray = GlbBuilder.glb_of(GlbBuilder.mesh_with(["m"]), "Thing")
	var facts: Dictionary = GlbInspect.inspect(glb)
	assert_true(facts["ok"] and facts["meshes"] == 1 and facts["triangles"] == 1 and facts["mesh_of_node"].has("Thing"), "static GLB: %s" % str(facts["problems"]))
	assert_true(not GlbInspect.inspect("not a glb".to_utf8_buffer())["ok"], "garbage")
	for bad: Dictionary in [{"buffers": [{"uri": "x.bin", "byteLength": 4}]}, {"skins": [{}]}, {"animations": [{}]}, {"extensionsRequired": ["X"]}]:
		assert_true(not GlbInspect.inspect(_glb_with(bad))["ok"], "rejects %s" % str(bad.keys()))
	var over: Array = GlbInspect.budget_warnings({"triangles": 300000, "nodes": 1, "materials": 1, "max_texture_px": 8192, "bytes": 10})
	assert_eq(over.size(), 2, "budget overruns are warnings")


func _glb_with(extra: Dictionary) -> PackedByteArray:
	var doc: Dictionary = {"asset": {"version": "2.0"}}
	doc.merge(extra)
	var json: PackedByteArray = JSON.stringify(doc).to_utf8_buffer()
	while json.size() % 4 != 0:
		json.append(32)
	var out := PackedByteArray()
	out.resize(20)
	out.encode_u32(0, 0x46546C67)
	out.encode_u32(4, 2)
	out.encode_u32(8, 20 + json.size())
	out.encode_u32(12, json.size())
	out.encode_u32(16, 0x4E4F534A)
	out.append_array(json)
	return out


func test_journal_roundtrip_and_stable_intent() -> void:
	var dir: String = tmp_dir("journal")
	var id: String = Journal.intent_id({"a": 1, "b": ["x"], "c": null})
	assert_eq(id, Journal.intent_id({"c": null, "b": ["x"], "a": 1}), "key order does not matter")
	assert_true(id != Journal.intent_id({"a": 2, "b": ["x"], "c": null}), "content matters")
	assert_true(Journal.find(dir, id).is_empty(), "empty journal")
	assert_eq(Journal.save(dir, id, {"state": "previewed", "idempotency_key": "asp_1"}), OK, "save")
	assert_eq(Journal.find(dir, id)["idempotency_key"], "asp_1", "find")
	assert_eq(Journal.save(dir, "other", {"state": "committed"}), OK, "second entry")
	assert_eq(Journal.entries(dir).size(), 2, "both kept")
	assert_true(Journal.new_key().length() == 36 and Journal.new_key() != Journal.new_key(), "keys are 8..100 chars and random")
	cleanup()


func _host() -> Node:
	return (Engine.get_main_loop() as SceneTree).root


func _collect(scene: String) -> RefCounted:
	var root: String = ProjectSettings.globalize_path("res://").simplify_path()
	return await Collector.collect(_host(), root, DATA + scene, {"managed_root": "res://assets/library"})


func test_collect_and_export_primitive_prop() -> void:
	var r: RefCounted = await _collect("prop.tscn")
	assert_true(r.ok, "collect: %s" % r.describe())
	if not r.ok:
		return
	var col: Dictionary = r.value
	assert_eq(col["entry_scene"], "tests/data/publish/prop.tscn", "entry scene package path")
	assert_eq((col["files"] as Array).size(), 2, "scene + material")
	assert_eq(col["resource_map"][DATA + "prop_mat.tres"]["path"], "tests/data/publish/prop_mat.tres", "resource_map")
	assert_eq(col["capabilities"], ["godot_text_scene_v1"], "no extra capabilities")
	assert_eq(col["placement"]["anchor"], ["0.25", "0", "-0.5"], "GroundAnchor is the anchor")
	assert_eq(col["slots"].size(), 1, "one slot")
	assert_eq(col["slots"][0]["source_surfaces"], [{"node_path": "Body", "surface": 0}], "source surface")
	var dir: String = tmp_dir("prop")
	var e: RefCounted = Export.export_glb(col, dir.path_join("portable.glb"))
	Collector.release(col)
	assert_true(e.ok, "export: %s" % e.describe())
	if e.ok:
		assert_eq(e.value["slots"][0]["portable"], [{"mesh": 0, "primitive": 0}], "portable surface")
		assert_true(e.value["facts"]["has_uv"] and e.value["facts"]["has_normal"], "UVs and normals")
		assert_eq(e.value["facts"]["meshes"], 1, "one mesh")
		var rep: Dictionary = Writer.conversion_report(e.value)
		assert_eq(rep["portable_status"], "exact", "exact")
		var pkg: RefCounted = Writer.write_package(col, rep, dir.path_join("source.zip"))
		assert_true(pkg.ok and (pkg.value["files"] as Array).size() == 2, "package: %s" % pkg.describe())
	cleanup()


func test_collect_and_export_csg_hut() -> void:
	var r: RefCounted = await _collect("hut.tscn")
	assert_true(r.ok, "collect: %s" % r.describe())
	if not r.ok:
		return
	var col: Dictionary = r.value
	assert_true(col["capabilities"].has("csg_static"), "csg_static detected")
	assert_eq(col["slots"].size(), 2, "walls + roof")
	assert_eq(col["slots"][0]["source_surfaces"], [{"node_path": "Shell/Walls", "surface": 0}], "CSG source surface")
	var dir: String = tmp_dir("hut")
	var e: RefCounted = Export.export_glb(col, dir.path_join("portable.glb"))
	Collector.release(col)
	assert_true(e.ok, "export: %s" % e.describe())
	if e.ok:
		assert_eq(e.value["facts"]["meshes"], 1, "CSG baked to one mesh")
		assert_eq(e.value["facts"]["primitives"], [3], "three baked surfaces")
		var ids: Array = []
		for s: Dictionary in e.value["slots"]:
			ids.append([s["slot_id"], s["portable"][0]["primitive"]])
		assert_eq(ids, [["walls", 0], ["roof", 2]], "slots point at the baked primitives of their materials")
	cleanup()


func test_collect_and_export_shader_is_disclosed() -> void:
	var r: RefCounted = await _collect("crystal.tscn")
	assert_true(r.ok, "collect: %s" % r.describe())
	if not r.ok:
		return
	var col: Dictionary = r.value
	assert_true(col["capabilities"].has("shader_source"), "shader_source")
	assert_eq((col["files"] as Array).size(), 4, "scene, material, shader, include")
	assert_true(col["resource_map"].has(DATA + "crystal.gdshaderinc"), "shader include is mapped")
	assert_true(not (col["warnings"] as Array).is_empty(), "desktop trust warning")
	var dir: String = tmp_dir("crystal")
	var e: RefCounted = Export.export_glb(col, dir.path_join("portable.glb"))
	Collector.release(col)
	assert_true(e.ok, "export: %s" % e.describe())
	if e.ok:
		var rep: Dictionary = Writer.conversion_report(e.value)
		assert_eq(rep["portable_status"], "approximated", "approximated")
		assert_eq(rep["approximations"][0]["slot_id"], "crystal_mat", "slot disclosed")
		var draft: Dictionary = Writer.descriptor_draft(col, e.value)
		assert_eq(draft["preview_warnings"], ["custom_shader_approximated"], "draft warning")
	cleanup()


func test_collect_blocks_scripts_and_animation() -> void:
	for c: Array in [["scripted.tscn", "scripts are not supported"], ["animated.tscn", "AnimationPlayer"]]:
		var r: RefCounted = await _collect(c[0])
		assert_true(not r.ok and r.code == "unsupported_source", "%s is blocked" % c[0])
		assert_true(str(r.details.get("problems", [])).contains(c[1]), "%s names the cause (%s)" % [c[0], str(r.details)])
	var missing: RefCounted = await _collect("nope.tscn")
	assert_true(not missing.ok and str(missing.details["problems"]).contains("save the scene first"), "missing file")
	var root: String = ProjectSettings.globalize_path("res://").simplify_path()
	var unsaved: RefCounted = await Collector.collect(_host(), root, DATA + "prop.tscn", {"unsaved": PackedStringArray([DATA + "prop_mat.tres"])})
	assert_true(not unsaved.ok and str(unsaved.details["problems"]).contains("unsaved changes"), "unsaved dependencies are reported")


func test_review_text_lists_validation_omissions_and_commit_hint() -> void:
	var review: Dictionary = {"phase": "previewed", "preview_id": "ipv_1", "capabilities": ["godot_text_scene_v1"],
			"target": {"mode": "new_version", "target_asset_id": "ast_x", "expected_current_version": "ver_y", "name": "N", "library": "prj_z"},
			"server": {"warnings": ["custom_shader_approximated"], "budget": {"within_ipad_budget": true}}, "local_warnings": ["local"],
			"conversion_report": {"portable_status": "approximated", "omissions": ["collision is source-only"],
					"approximations": [{"slot_id": "s", "reason": "shader replaced"}]},
			"descriptor_draft": {"placement_anchor": ["0", "0", "0"], "footprint_radius_m": "1", "material_slots": [{}], "collision": null}}
	var text: String = Dialog.review_text(review)
	for needle: String in ["New version of ast_x (base ver_y)", "custom_shader_approximated", "omitted: collision is source-only",
			"approximated (s): shader replaced", "Nothing is published until you press Commit"]:
		assert_true(text.contains(needle), needle)
