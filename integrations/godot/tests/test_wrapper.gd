extends "res://tests/project_test_base.gd"
# Wrapper building (anchor offset, editable instance only with overrides, deterministic bytes) and the
# hand-edit conflict check.

const Wrapper = preload("res://addons/assetstudio/project/as_wrapper.gd")
const State = preload("res://addons/assetstudio/project/as_project_state.gd")
const Coordinator = preload("res://addons/assetstudio/project/as_mutation_coordinator.gd")
const GlbBuilder = preload("res://tests/glb_builder.gd")

const KEY: String = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


## A model scene saved under user:// (so the wrapper has a path to reference), loaded back as a PackedScene.
func _model_scene() -> PackedScene:
	var model: Node = GlbBuilder.scene_of(GlbBuilder.glb_of(GlbBuilder.mesh_with(["a", "b"]), "tree"))
	var packed := PackedScene.new()
	for n: Node in model.get_children():
		n.owner = model
	packed.pack(model)
	model.free()
	var path: String = "user://as_test_model_%d.tscn" % randi()
	ResourceSaver.save(packed, path)
	return ResourceLoader.load(path, "", ResourceLoader.CACHE_MODE_IGNORE) as PackedScene


func _override(model: Node3D) -> RefCounted:
	var mi: MeshInstance3D = model.get_child(0) as MeshInstance3D
	var m := StandardMaterial3D.new()
	m.metallic = 0.5
	m.resource_scene_unique_id = "patched_s_0"
	mi.set_surface_override_material(1, m)
	return load("res://addons/assetstudio/core/as_errors.gd").success({"overrides": 1})


func _text(r: RefCounted) -> String:
	return (r.value["bytes"] as PackedByteArray).get_string_from_utf8()


func test_wrapper_with_overrides_is_editable_and_deterministic() -> void:
	var scene: PackedScene = _model_scene()
	var a: RefCounted = Wrapper.build_bytes("b-1", KEY, scene, ["0.25", "0", "-0.5"], _override)
	var b: RefCounted = Wrapper.build_bytes("b-1", KEY, scene, ["0.25", "0", "-0.5"], _override)
	assert_true(a.ok and b.ok, "built: %s" % a.describe())
	assert_eq(a.value["bytes"], b.value["bytes"], "same inputs, same bytes")
	var text: String = _text(a)
	assert_true(text.contains("[editable path=\"Model\"]"), "editable instance when overrides exist")
	assert_true(text.contains("surface_material_override/1"), "override stored on the imported node")
	assert_true(text.contains("id=\"patched_s_0\""), "patched material is an embedded sub_resource with a stable id")
	assert_true(text.contains("metadata/assetstudio_binding = \"b-1\""), "binding metadata")
	assert_true(text.contains("-0.25, 0, 0.5"), "Model sits at -anchor: %s" % text)
	assert_true(not text.contains("unique_id=") and not text.contains("uid="), "engine-random ids stripped")


func test_wrapper_without_overrides_is_not_editable() -> void:
	var r: RefCounted = Wrapper.build_bytes("b-2", KEY, _model_scene(), ["0", "0", "0"], Callable())
	assert_true(r.ok, "built")
	assert_true(not _text(r).contains("editable"), "no editable instance without overrides")
	assert_true(not _text(r).contains("surface_material_override"), "no overrides")


func test_conflict_detection() -> void:
	var root: String = tmp_dir("wrap")
	var rel: String = "assets/prefabs/b-3.tscn"
	assert_eq(Wrapper.check_conflict(root, "b-3", rel), "", "no file: free to write")
	var bytes: PackedByteArray = "[gd_scene format=3]\n".to_utf8_buffer()
	Fs.write_atomic(root.path_join(rel), bytes)
	assert_true(Wrapper.check_conflict(root, "b-3", rel) != "", "file the addon never recorded")
	var c: RefCounted = Coordinator.new(root)
	assert_true(c.open("t").ok, "open")
	c.add_write(State.WRAPPERS_REL, State.wrappers_bytes(root, "b-3", rel, Fs.sha256_bytes(bytes)))
	assert_true(c.commit().ok, "commit")
	assert_eq(Wrapper.check_conflict(root, "b-3", rel), "", "recorded and unmodified")
	Fs.write_atomic(root.path_join(rel), "[gd_scene format=3]\n[node name=\"Hand\" type=\"Node3D\"]\n".to_utf8_buffer())
	assert_true(Wrapper.check_conflict(root, "b-3", rel).contains("modified"), "hand edit is a conflict")
	cleanup()
