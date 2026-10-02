extends "res://tests/project_test_base.gd"
# Material profile parsing (strict), rule evaluation, patch allowlist and application on an in-memory scene.

const MaterialPolicy = preload("res://addons/assetstudio/project/as_material_policy.gd")
const SlotResolver = preload("res://addons/assetstudio/project/as_slot_resolver.gd")
const GlbBuilder = preload("res://tests/glb_builder.gd")

const SOLID: String = "res://tests/data/mat_solid.tres"
const SLOTS: Array = [
	{"slot_id": "m_solid", "role": "solid", "surfaces": {"portable_glb_v1": [{"mesh": 0, "primitive": 0}]}},
	{"slot_id": "m_foliage", "role": "foliage", "surfaces": {"portable_glb_v1": [{"mesh": 0, "primitive": 1}]}},
	{"slot_id": "m_other", "role": "misc", "surfaces": {"portable_glb_v1": [{"mesh": 0, "primitive": 1}]}},
]


func _profile(rules: Array, id: String = "p1") -> PackedByteArray:
	return JSON.stringify({"schema_version": 1, "profile_id": id, "rules": rules}).to_utf8_buffer()


func _parse(rules: Array, load_materials: bool = false) -> RefCounted:
	return MaterialPolicy.parse(_profile(rules), "p1", load_materials)


func test_valid_profile_parses_and_hashes_raw_bytes() -> void:
	var raw: PackedByteArray = _profile([
		{"match": {"role": "foliage"}, "material": SOLID},
		{"match": {"slot_id": "m_solid"}, "material": SOLID},
		{"match": {"role": "solid"}, "patch": {"metallic": 0.0, "metallic_texture": null, "roughness": 1,
				"cull_mode": 2, "vertex_color_use_as_albedo": true, "albedo_color": "#ff8800",
				"transparency": 1, "alpha_scissor_threshold": 0.5}}])
	var r: RefCounted = MaterialPolicy.parse(raw, "p1", true)
	assert_true(r.ok, "parse: %s" % r.describe())
	assert_eq(r.value["rules"].size(), 3, "three rules")
	assert_eq(r.value["sha256"], Fs.sha256_bytes(raw), "sha256 of the raw file bytes")
	assert_true(r.value["rules"][0]["material"] is Material, "material loaded")


func test_invalid_profiles_are_rejected() -> void:
	var bad_docs: Array = [
		"not json".to_utf8_buffer(),
		JSON.stringify({"schema_version": 2, "profile_id": "p1", "rules": []}).to_utf8_buffer(),
		JSON.stringify({"schema_version": 1, "profile_id": "other", "rules": []}).to_utf8_buffer(),
		JSON.stringify({"schema_version": 1, "profile_id": "p1", "rules": [], "extra": 1}).to_utf8_buffer(),
		JSON.stringify({"schema_version": 1, "profile_id": "p1"}).to_utf8_buffer(),
		JSON.stringify({"schema_version": 1, "profile_id": "p1", "rules": {}}).to_utf8_buffer(),
	]
	for raw: PackedByteArray in bad_docs:
		assert_true(not MaterialPolicy.parse(raw, "p1", false).ok, "rejected: %s" % raw.get_string_from_utf8().left(50))


func test_invalid_rules_are_rejected() -> void:
	var bad_rules: Array = [
		{"match": {"role": "x"}},
		{"match": {"role": "x"}, "material": SOLID, "patch": {"metallic": 0.1}},
		{"match": {}, "material": SOLID},
		{"match": {"colour": "x"}, "material": SOLID},
		{"match": {"role": "X Y"}, "material": SOLID},
		{"match": {"role": "x"}, "material": "http://evil/x.tres"},
		{"match": {"role": "x"}, "material": "res://../x.tres"},
		{"match": {"role": "x"}, "material": "user://x.tres"},
		{"match": {"role": "x"}, "patch": {}},
		{"match": {"role": "x"}, "patch": {"shading_mode": 0}},
		{"match": {"role": "x"}, "patch": {"metallic_texture": "res://t.png"}},
		{"match": {"role": "x"}, "patch": {"metallic": 2}},
		{"match": {"role": "x"}, "patch": {"cull_mode": 7}},
		{"match": {"role": "x"}, "patch": {"vertex_color_use_as_albedo": "yes"}},
		{"match": {"role": "x"}, "patch": {"albedo_color": "nothex"}},
		{"match": {"role": "x"}, "patch": {"transparency": 1.5}},
		{"match": {"role": "x"}, "patch": {"roughness": 0.5}, "extra": 1},
		"not an object",
	]
	for rule: Variant in bad_rules:
		assert_true(not _parse([rule]).ok, "rejected rule: %s" % str(rule))


func test_materials_must_exist_and_be_materials() -> void:
	assert_true(not _parse([{"match": {"role": "x"}, "material": "res://tests/data/missing.tres"}], true).ok, "missing")
	assert_true(not _parse([{"match": {"role": "x"}, "material": "res://tests/data/not_a_material.tres"}], true).ok, "not a Material")
	assert_true(_parse([{"match": {"role": "x"}, "material": SOLID}], true).ok, "real material")


func test_first_matching_rule_wins_per_slot() -> void:
	var rules: Array = _parse([
		{"match": {"slot_id": "m_other", "role": "misc"}, "patch": {"roughness": 0.1}},
		{"match": {"role": "foliage"}, "material": SOLID},
		{"match": {"role": "foliage"}, "patch": {"roughness": 0.9}},
		{"match": {"slot_id": "m_other"}, "patch": {"roughness": 0.2}}]).value["rules"]
	var hits: Array = MaterialPolicy.evaluate(rules, SLOTS)
	assert_eq(hits[0]["rule"], -1, "m_solid unmatched")
	assert_eq(hits[1]["rule"], 1, "foliage -> first foliage rule")
	assert_eq(hits[2]["rule"], 0, "m_other -> rule 0 (both keys)")


func _model() -> Node:
	var glb: PackedByteArray = GlbBuilder.glb_of(GlbBuilder.mesh_with(["M_solid", "M_foliage"]), "tree")
	return GlbBuilder.scene_of(glb)


func _mapping(model: Node) -> Dictionary:
	var meshes: RefCounted = SlotResolver.gltf_meshes(GlbBuilder.glb_of(GlbBuilder.mesh_with(["M_solid", "M_foliage"]), "tree"))
	return SlotResolver.resolve(meshes.value, model, SLOTS).value["slots"]


func test_apply_sets_overrides_patches_source_and_reports_unmapped() -> void:
	var model: Node = _model()
	var rules: Array = _parse([
		{"match": {"role": "foliage"}, "material": SOLID},
		{"match": {"role": "solid"}, "patch": {"metallic": 0.25, "roughness": 0.75, "albedo_color": [1, 0, 0]}}], true).value["rules"]
	var r: RefCounted = MaterialPolicy.apply(model, rules, SLOTS, _mapping(model))
	assert_true(r.ok, "apply: %s" % r.describe())
	assert_eq(r.value["overrides"], 2, "two surfaces overridden")
	assert_eq(r.value["unmapped"], ["m_other"], "the slot without a rule is reported")
	var mi: MeshInstance3D = model.get_child(0) as MeshInstance3D
	var patched: BaseMaterial3D = mi.get_surface_override_material(0) as BaseMaterial3D
	assert_true(patched != null and absf(patched.metallic - 0.25) < 0.001, "patched metallic on surface 0")
	assert_eq(patched.albedo_color, Color(1, 0, 0, 1), "patched colour")
	assert_true(patched != mi.mesh.surface_get_material(0), "the source material is duplicated, not edited")
	assert_eq(patched.resource_name, "M_solid", "the source material name survives the duplicate")
	assert_eq(mi.mesh.surface_get_material(0).metallic, 0.0, "source untouched")
	assert_true(mi.get_surface_override_material(1) is StandardMaterial3D and mi.get_surface_override_material(1).albedo_color.is_equal_approx(Color(0.2, 0.4, 0.6)), "material rule on surface 1")
	model.free()


func test_patch_without_a_source_material_starts_from_a_standard_material() -> void:
	var glb: PackedByteArray = GlbBuilder.glb_of(GlbBuilder.mesh_with([""]), "prop")
	var model: Node = GlbBuilder.scene_of(glb)
	var slots: Array = [{"slot_id": "s", "role": "surface", "surfaces": {"portable_glb_v1": [{"mesh": 0, "primitive": 0}]}}]
	var mapping: Dictionary = SlotResolver.resolve(SlotResolver.gltf_meshes(glb).value, model, slots).value["slots"]
	var rules: Array = _parse([{"match": {"role": "surface"}, "patch": {"metallic_texture": null, "cull_mode": 0}}]).value["rules"]
	var r: RefCounted = MaterialPolicy.apply(model, rules, slots, mapping)
	assert_true(r.ok and r.value["overrides"] == 1, "patched")
	var mi: MeshInstance3D = model.get_child(0) as MeshInstance3D
	assert_eq((mi.get_surface_override_material(0) as BaseMaterial3D).cull_mode, BaseMaterial3D.CULL_BACK, "cull mode 0")
	model.free()
