extends "res://tests/project_test_base.gd"
# Dock-side logic that needs no editor: placement and instance swap through a plain UndoRedo, drag payload, binding
# states, descriptor diff and the rollback history lookup.

const Place = preload("res://addons/assetstudio/editor/as_place.gd")
const DragAdapter = preload("res://addons/assetstudio/editor/as_drag_adapter.gd")
const BindingState = preload("res://addons/assetstudio/project/as_binding_state.gd")
const DescriptorDiff = preload("res://addons/assetstudio/project/as_descriptor_diff.gd")
const UpdateCommand = preload("res://addons/assetstudio/project/as_update_command.gd")
const Descriptor = preload("res://addons/assetstudio/core/as_asset_descriptor.gd")
const UpdateDialog = preload("res://addons/assetstudio/editor/as_update_dialog.gd")


func _wrapper(binding_id: String, marker: String) -> PackedScene:
	var root := Node3D.new()
	root.name = "W"
	root.set_meta("assetstudio_binding", binding_id)
	root.set_meta("marker", marker)
	var packed := PackedScene.new()
	packed.pack(root)
	root.free()
	return packed


func _scene() -> Array:
	var root := Node3D.new()
	root.name = "Level"
	var holder := Node3D.new()
	holder.name = "Holder"
	holder.position = Vector3(10, 0, 0)
	root.add_child(holder)
	holder.owner = root
	return [root, holder]


func test_place_adds_owned_child_at_the_point_and_undoes() -> void:
	var s: Array = _scene()
	var undo := UndoRedo.new()
	var node: Node = Place.place(undo, s[0], s[0], _wrapper("b1", "v1"), Vector3(1, 2, 3))
	assert_true(node.get_parent() == s[0], "child of the scene root")
	assert_true(node.owner == s[0], "owned by the scene root (saved with the scene)")
	assert_eq((node as Node3D).position, Vector3(1, 2, 3), "position")
	undo.undo()
	assert_eq(s[0].get_child_count(), 1, "undo removes the instance")
	undo.redo()
	assert_eq(s[0].get_child_count(), 2, "redo re-adds it")
	assert_true(node.owner == s[0], "owner restored on redo")
	s[0].free()
	undo.free()


func test_swap_instances_keeps_transform_name_and_index_and_undoes() -> void:
	var s: Array = _scene()
	var undo := UndoRedo.new()
	var olds: Array = []
	for i: int in 2:
		var o: Node3D = _wrapper("b1", "old").instantiate()
		o.name = "Tree%d" % i
		o.position = Vector3(i, 0, 5)
		o.rotation.y = 0.5 * i
		s[0].add_child(o)
		o.owner = s[0]
		olds.append(o)
	var other: Node3D = _wrapper("b9", "other").instantiate()
	other.name = "Other"
	s[0].add_child(other)
	other.owner = s[0]
	var picked: Array = Place.instances_of(s[0].get_children(), "b1")
	assert_eq(picked.size(), 2, "only b1 instances are picked")
	var news: Array = Place.swap_instances(undo, s[0], picked, _wrapper("b2", "new"))
	assert_eq(news.size(), 2, "two new instances")
	for i: int in 2:
		assert_eq(str(news[i].get_meta("marker")), "new", "new wrapper")
		assert_eq(news[i].name, olds[i].name, "name kept")
		assert_eq(news[i].transform, olds[i].transform, "transform kept")
		assert_true(news[i].owner == s[0] and news[i].get_parent() == s[0], "owned, in the scene")
	assert_eq(str(s[0].get_node("Other").get_meta("marker")), "other", "other binding untouched")
	assert_eq(news[0].get_index(), olds[0].get_index() if olds[0].get_parent() != null else 1, "index kept")
	undo.undo()
	assert_eq(str(s[0].get_node("Tree0").get_meta("marker")), "old", "undo restores the old instance")
	assert_true(olds[1].owner == s[0], "owner restored")
	undo.redo()
	assert_eq(str(s[0].get_node("Tree1").get_meta("marker")), "new", "redo swaps again")
	s[0].free()
	for o: Node in olds:
		if not o.is_inside_tree():
			o.free()
	undo.free()


func test_drag_payload_only_for_ready() -> void:
	var path: String = "res://tests/data/mat_solid.tres"
	for st: String in ["remote", "downloading", "preparing", "unavailable", "unsupported"]:
		assert_eq(DragAdapter.payload_for({"state": st, "wrapper_res": path}), null, "%s is not draggable" % st)
	assert_eq(DragAdapter.payload_for({"state": "ready", "wrapper_res": "res://tests/data/nope.tscn"}), null, "missing wrapper")
	assert_eq(DragAdapter.payload_for(null), null, "no entry")
	var p: Variant = DragAdapter.payload_for({"state": "ready", "wrapper_res": path})
	assert_eq(p["type"], "files", "native files payload")
	assert_eq(p["files"], PackedStringArray([path]), "wrapper path")
	assert_true(DragAdapter.payload_for({"state": "update_available", "wrapper_res": path}) != null, "an installed version stays draggable")


func test_descriptor_diff_between_fixture_versions() -> void:
	var a: Dictionary = Descriptor.parse_bytes(fixture("descriptors/valid/primitive_prop.json")).value.data
	var b: Dictionary = Descriptor.parse_bytes(fixture("descriptors/valid/primitive_prop_v2.json")).value.data
	var lines: PackedStringArray = DescriptorDiff.diff(a, b)
	var text: String = "\n".join(lines)
	assert_true(text.contains("placement_anchor"), "anchor change: %s" % text)
	assert_true(text.contains("slot added: lid"), "slot added")
	assert_eq(DescriptorDiff.diff(a, a).size(), 0, "no diff against itself")
	var review: Dictionary = {"binding_id": "b", "from": "v1", "to": "v2", "diff": lines, "instances": 2}
	assert_true(UpdateDialog.summary_text(review).contains("Instances in the open scene: 2"), "dialog text")


func test_last_move_reads_history_summaries() -> void:
	var root: String = tmp_dir("hist")
	var doc: Dictionary = {"schema_version": 1, "entries": [
		{"id": "t1", "operation": "update", "ops": [], "time": "x", "summary": {"kind": "update", "mode": "rewrite", "binding_id": "b", "from_key": "k1", "to_key": "k2", "from_deps": {}}},
		{"id": "t2", "operation": "add", "ops": [], "time": "x", "summary": {"kind": "add", "binding_id": "c", "to_key": "k9"}},
		{"id": "t3", "operation": "finalize", "ops": [], "time": "x", "summary": {"kind": "finalize", "bindings": ["b"]}},
	]}
	Fs.write_atomic(root.path_join(".assetstudio/history.json"), CJson.encode(doc).value)
	assert_eq(UpdateCommand.last_move(root, "b", "k2")["from_key"], "k1", "previous version found")
	assert_true(UpdateCommand.last_move(root, "b", "other").is_empty(), "current key differs: nothing to roll back")
	assert_true(UpdateCommand.last_move(root, "c", "k9").is_empty(), "an add is not a move")
	cleanup()
