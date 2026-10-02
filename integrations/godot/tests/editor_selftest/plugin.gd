@tool
extends EditorPlugin
# Test-only plugin (copied into a consumer project by run_plugin_tests.py). Runs the real dock actions inside a
# headless editor against the fake server: install -> import -> finalize -> place (undo/redo) -> update detection ->
# review -> update selected instances -> update binding -> restore previous. Prints "STEP <name> PASS|FAIL" lines
# and "SELFTEST_OK" / "SELFTEST_FAILED", then quits.

const Actions = preload("res://addons/assetstudio/editor/as_dock_actions.gd")
const Place = preload("res://addons/assetstudio/editor/as_place.gd")
const Dock = preload("res://addons/assetstudio/editor/as_dock.gd")

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
