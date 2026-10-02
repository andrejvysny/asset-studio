@tool
extends EditorPlugin
# AssetStudio editor plugin (design §8): crash recovery of an interrupted install/update, then the dock.
# Nothing is networked until a connection is configured (`connect` command); the dock shows its status instead.

const Coordinator = preload("res://addons/assetstudio/project/as_mutation_coordinator.gd")
const Dock = preload("res://addons/assetstudio/editor/as_dock.gd")

var _dock: Control = null


func _enter_tree() -> void:
	var root: String = ProjectSettings.globalize_path("res://").simplify_path()
	var rec: RefCounted = Coordinator.recover_project(root)
	if not rec.ok:
		push_warning("AssetStudio: transaction recovery failed: %s" % rec.describe())
	_dock = Dock.new()
	add_control_to_dock(DOCK_SLOT_LEFT_BR, _dock)  # in the tree first: setup() starts awaiting network calls
	_dock.setup(self)


func _exit_tree() -> void:
	if _dock != null:
		remove_control_from_docks(_dock)
		_dock.queue_free()
		_dock = null
