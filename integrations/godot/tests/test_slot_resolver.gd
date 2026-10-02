extends "res://tests/project_test_base.gd"
# Slot resolution on fixture GLBs, a named two-primitive material GLB (nature-kit style) and an unnamed single
# material prop.

const SlotResolver = preload("res://addons/assetstudio/project/as_slot_resolver.gd")
const Descriptor = preload("res://addons/assetstudio/core/as_asset_descriptor.gd")
const GlbBuilder = preload("res://tests/glb_builder.gd")


func _slots_of(desc_fixture: String) -> Array:
	return Descriptor.parse_bytes(fixture(desc_fixture)).value.data["material_slots"]


func _resolve(glb: PackedByteArray, slots: Array) -> Dictionary:
	var meshes: RefCounted = SlotResolver.gltf_meshes(glb)
	assert_true(meshes.ok, "gltf_meshes: %s" % meshes.describe())
	var scene: Node = GlbBuilder.scene_of(glb)
	assert_true(scene != null, "scene")
	var r: RefCounted = SlotResolver.resolve(meshes.value, scene, slots)
	assert_true(r.ok, "resolve")
	var out: Dictionary = r.value
	# resolve() only reports paths, so the scene can go
	out["scene_children"] = scene.get_child_count()
	scene.free()
	return out


func test_fixture_v1_single_primitive_resolves() -> void:
	var out: Dictionary = _resolve(fixture("glb/primitive_prop.portable.glb"), _slots_of("descriptors/valid/primitive_prop.json"))
	assert_eq(out["unresolved"].size(), 0, "nothing unresolved")
	assert_eq(out["slots"]["body"].size(), 1, "body has one target")
	assert_eq(out["slots"]["body"][0]["surface"], 0, "surface 0")


func test_fixture_v2_two_meshes_resolve_to_different_nodes() -> void:
	var out: Dictionary = _resolve(fixture("glb/primitive_prop_v2.portable.glb"), _slots_of("descriptors/valid/primitive_prop_v2.json"))
	assert_eq(out["unresolved"].size(), 0, "nothing unresolved")
	assert_eq(out["slots"]["body"].size(), 1, "body")
	assert_eq(out["slots"]["lid"].size(), 1, "lid")
	assert_true(out["slots"]["body"][0]["path"] != out["slots"]["lid"][0]["path"], "different nodes")


func test_named_two_primitive_materials_map_to_surfaces() -> void:
	var glb: PackedByteArray = GlbBuilder.glb_of(GlbBuilder.mesh_with(["M_solid", "M_foliage"]), "tree")
	var slots: Array = [
		{"slot_id": "m_solid", "role": "solid", "surfaces": {"portable_glb_v1": [{"mesh": 0, "primitive": 0}]}},
		{"slot_id": "m_foliage", "role": "foliage", "surfaces": {"portable_glb_v1": [{"mesh": 0, "primitive": 1}]}},
	]
	var out: Dictionary = _resolve(glb, slots)
	assert_eq(out["unresolved"].size(), 0, "nothing unresolved")
	assert_eq(out["slots"]["m_solid"][0]["surface"], 0, "solid is primitive 0")
	assert_eq(out["slots"]["m_foliage"][0]["surface"], 1, "foliage is primitive 1")
	assert_eq(out["slots"]["m_solid"][0]["path"], out["slots"]["m_foliage"][0]["path"], "same mesh node")


func test_unnamed_single_material_prop() -> void:
	var glb: PackedByteArray = GlbBuilder.glb_of(GlbBuilder.mesh_with([""]), "prop")
	var slots: Array = [{"slot_id": "surface", "role": "surface", "surfaces": {"portable_glb_v1": [{"mesh": 0, "primitive": 0}]}}]
	var out: Dictionary = _resolve(glb, slots)
	assert_eq(out["unresolved"].size(), 0, "resolved by name or order")
	assert_eq(out["slots"]["surface"].size(), 1, "one target")


func test_unresolvable_surfaces_are_reported() -> void:
	var glb: PackedByteArray = GlbBuilder.glb_of(GlbBuilder.mesh_with(["a"]), "prop")
	var slots: Array = [
		{"slot_id": "ghost", "role": "x", "surfaces": {"portable_glb_v1": [{"mesh": 5, "primitive": 0}]}},
		{"slot_id": "deep", "role": "x", "surfaces": {"portable_glb_v1": [{"mesh": 0, "primitive": 3}]}},
		{"slot_id": "static_only", "role": "x", "surfaces": {"godot_static_source_v1": [{"node_path": "A", "surface": 0}]}},
	]
	var out: Dictionary = _resolve(glb, slots)
	assert_eq(out["unresolved"].size(), 3, "all three reported")
	assert_eq(out["slots"]["ghost"].size(), 0, "no targets")


func test_rejects_non_glb_bytes() -> void:
	assert_true(not SlotResolver.gltf_meshes(PackedByteArray([1, 2, 3])).ok, "short")
	var glb: PackedByteArray = fixture("glb/primitive_prop.portable.glb")
	glb[0] = 0
	assert_true(not SlotResolver.gltf_meshes(glb).ok, "bad magic")
	var truncated: PackedByteArray = fixture("glb/primitive_prop.portable.glb").slice(0, 100)
	assert_true(not SlotResolver.gltf_meshes(truncated).ok, "length mismatch")
