@tool
extends EditorPlugin
# Test-only plugin (copied into a consumer project by run_plugin_tests.py). Runs the real dock actions inside a
# headless editor against the fake server: install -> import -> finalize -> place (undo/redo) -> update detection ->
# review -> update selected instances -> update binding -> restore previous. Prints "STEP <name> PASS|FAIL" lines
# and "SELFTEST_OK" / "SELFTEST_FAILED", then quits.

const Actions = preload("res://addons/assetstudio/editor/as_dock_actions.gd")
const Place = preload("res://addons/assetstudio/editor/as_place.gd")
const Dock = preload("res://addons/assetstudio/editor/as_dock.gd")
const PublishActions = preload("res://addons/assetstudio/editor/as_publish_actions.gd")
const PublishDialog = preload("res://addons/assetstudio/editor/as_publish_dialog.gd")

const LIBRARY: String = "prj_0000000000000001"
const ASSET: String = "ast_00000000000000aa"
const V1: String = "ver_00000000000000v1"
const V2: String = "ver_00000000000000v2"

var _failed: int = 0
var _actions: Node = null


func _enter_tree() -> void:
	if OS.get_environment("ASSETSTUDIO_SELFTEST") == "1":
		_run.call_deferred()


func _step(name: String, ok: bool, detail: String = "") -> void:
	print("STEP %s %s %s" % [name, "PASS" if ok else "FAIL", detail if not ok else ""])
	if not ok:
		_failed += 1


func _run() -> void:
	await get_tree().create_timer(1.0).timeout
	_actions = Actions.new()
	add_child(_actions)
	_actions.setup(self)
	_actions.import_wait_s = 90.0
	_step("connected", _actions.connection_status()["connected"], str(_actions.connection_status()))
	EditorInterface.open_scene_from_path("res://level.tscn")
	await get_tree().create_timer(1.0).timeout
	var scene_root: Node = EditorInterface.get_edited_scene_root()
	_step("scene open", scene_root != null)
	await _dock_lists_items()
	if scene_root != null:
		await _scenario(scene_root)
		await _publish_steps()
	print("SELFTEST_%s" % ("OK" if _failed == 0 else "FAILED"))
	get_tree().quit(1 if _failed != 0 else 0)


func _dock_lists_items() -> void:
	var dock: Control = Dock.new()
	add_child(dock)
	dock.setup(self)
	await get_tree().create_timer(3.0).timeout
	_step("dock lists the server asset with its state badge", dock._list.item_count == 1
			and dock._list.get_item_text(0).contains("Fixture Crate") and dock._list.get_item_text(0).contains("[Remote]"),
			str(dock._list.item_count))
	dock.queue_free()


func _scenario(scene_root: Node) -> void:
	var item: Dictionary = {"asset_id": ASSET, "library_id": LIBRARY, "current_version_id": V1}
	var r: RefCounted = await _actions.install(item)
	_step("install (add, import, finalize)", r.ok, r.describe())
	var bid: String = _first_binding()
	_step("binding is Ready with a wrapper", bid != "" and _actions.infos[bid]["state"] == "ready"
			and FileAccess.file_exists(_actions.infos[bid]["wrapper_res"]), str(_actions.infos))
	if bid == "":
		return
	var placed: Node = _actions.place(bid)
	_step("place adds an owned instance", placed != null and placed.get_parent() == scene_root and placed.owner == scene_root
			and str(placed.get_meta("assetstudio_binding", "")) == bid)
	var hist: UndoRedo = get_undo_redo().get_history_undo_redo(get_undo_redo().get_object_history_id(scene_root))
	hist.undo()
	_step("undo removes the placed instance", scene_root.get_child_count() == 0, str(scene_root.get_child_count()))
	hist.redo()
	_step("redo restores it", scene_root.get_child_count() == 1)
	placed = scene_root.get_child(0)
	await _updates(scene_root, bid, placed, hist)


func _updates(scene_root: Node, bid: String, placed: Node, hist: UndoRedo) -> void:
	await _actions.check_updates()
	_step("update badge (server current is v2)", _actions.infos[bid]["state"] == "update_available"
			and _actions.infos[bid]["target_version"] == V2, str(_actions.infos[bid]))
	var review: RefCounted = await _actions.review(bid)
	_step("review shows the descriptor diff", review.ok and str(review.value["diff"]).contains("slot added: lid")
			and review.value["instances"] == 1, review.describe())
	EditorInterface.get_selection().clear()
	EditorInterface.get_selection().add_node(placed)
	var r: RefCounted = await _actions.apply_update_instances(bid)
	_step("update selected instances", r.ok, r.describe())
	var swapped: Node = scene_root.get_child(0)
	var new_id: String = str(swapped.get_meta("assetstudio_binding", ""))
	_step("instance now uses the new binding, old binding intact", new_id != bid and _actions.infos.has(bid)
			and _actions.infos[bid]["version_id"] == V1, new_id)
	hist.undo()
	_step("undo swaps back", str(scene_root.get_child(0).get_meta("assetstudio_binding", "")) == bid)
	await _binding_updates(bid)


func _binding_updates(bid: String) -> void:
	await _actions.check_updates()
	var r: RefCounted = await _actions.apply_update_binding(bid)
	_step("update binding (all instances)", r.ok and _actions.infos[bid]["version_id"] == V2
			and _actions.infos[bid]["state"] == "ready", r.describe() + str(_actions.infos.get(bid)))
	_step("restore previous is offered", _actions.can_restore_previous(bid))
	r = await _actions.restore_previous(bid)
	_step("restore previous version", r.ok and _actions.infos[bid]["version_id"] == V1, r.describe())
	_actions.dismiss(bid)
	_step("dismiss hides the badge", _actions.infos[bid]["state"] == "ready", str(_actions.infos[bid]))


func _first_binding() -> String:
	_actions.reload()
	for b: String in _actions.infos:
		if _actions.infos[b]["version_id"] == V1:
			return b
	return ""


const PUB_SCENE: String = """[gd_scene load_steps=3 format=3]

[sub_resource type="BoxMesh" id="BoxMesh_1"]

[sub_resource type="StandardMaterial3D" id="Mat_1"]
albedo_color = Color(0.2, 0.6, 0.3, 1)

[node name="PubProp" type="Node3D"]

[node name="Body" type="MeshInstance3D" parent="."]
mesh = SubResource("BoxMesh_1")
surface_material_override/0 = SubResource("Mat_1")

[node name="Group" type="Node3D" parent="."]
transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 2, 0, 0)

[node name="Inner" type="MeshInstance3D" parent="Group"]
mesh = SubResource("BoxMesh_1")
"""


func _fingerprint(n: Node) -> String:
	var parts: PackedStringArray = [str(n.get_path()), n.get_class(), str((n as Node3D).transform) if n is Node3D else ""]
	for c: Node in n.get_children():
		parts.append(_fingerprint(c))
	return "|".join(parts)


## AS-09: publish from the edited (saved) scene through the dock action code; the open scene must not change.
func _publish_steps() -> void:
	var path: String = "res://pub_scene.tscn"
	var f: FileAccess = FileAccess.open(path, FileAccess.WRITE)
	f.store_string(PUB_SCENE)
	f.close()
	EditorInterface.open_scene_from_path(path)
	await get_tree().create_timer(1.0).timeout
	var edited: Node = EditorInterface.get_edited_scene_root()
	_step("publish scene is open", edited != null and edited.scene_file_path == path)
	if edited == null:
		return
	var before: String = _fingerprint(edited)
	var pub: Node = PublishActions.new()
	add_child(pub)
	pub.setup()
	pub.start(LIBRARY, {})
	_step("the publish form opens for a saved scene", pub.dialog.visible and pub.dialog.stage == "form")
	var r: RefCounted = await pub.preview({"name": "Selftest Prop", "tags": "", "licence": "", "category": "", "selection": false, "new_version": false})
	_step("build + preview from the edited scene", r.ok and pub.dialog.stage == "review", r.describe())
	if r.ok:
		_step("review shows validation and requires an explicit commit", PublishDialog.review_text(pub._prep["review"]).contains("Nothing is published until"))
		r = await pub.commit()
		_step("explicit commit publishes", r.ok and r.value["outcome"]["asset_id"] != null, r.describe())
	_step("the open scene is unchanged and not marked unsaved", _fingerprint(edited) == before
			and not EditorInterface.get_unsaved_scenes().has(path))
	await _publish_selection(pub, edited, before)
	pub.queue_free()


func _publish_selection(pub: Node, edited: Node, before: String) -> void:
	EditorInterface.get_selection().clear()
	EditorInterface.get_selection().add_node(edited.get_node("Group"))
	pub.start(LIBRARY, {})
	var r: RefCounted = await pub.preview({"name": "Selftest Group", "tags": "", "licence": "", "category": "", "selection": true, "new_version": false})
	_step("a selected subtree is published from a temporary scene", r.ok and str(pub._opts["scene"]).contains("selection_group"), r.describe())
	if r.ok:
		r = await pub.commit()
		_step("selection commit", r.ok, r.describe())
	_step("the open scene is still unchanged after a selection publish", _fingerprint(edited) == before
			and not FileAccess.file_exists(ProjectSettings.globalize_path("res://.assetstudio/publish/selection_group.tscn")))
