@tool
extends Node
# Editor-side orchestration of "Publish scene..." (AS-09). It adds only what needs the editor (the edited scene's
# saved path, unsaved-scene detection, the selection, the dialog); the build, preview and commit are the same
# project/as_publish_command.gd functions the CLI uses. The open scene is never modified: the saved file (or, for a
# selection, a temporary scene saved from a DUPLICATE of the selected subtree) is what gets collected.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Commands = preload("res://addons/assetstudio/project/as_commands.gd")
const PublishCommand = preload("res://addons/assetstudio/project/as_publish_command.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")
const Graph = preload("res://addons/assetstudio/project/as_source_graph.gd")
const Dialog = preload("res://addons/assetstudio/editor/as_publish_dialog.gd")

signal message(text: String)

var root: String = ""
var dialog: ConfirmationDialog = null

var _cmd: RefCounted = null
var _library: String = ""
var _base: Dictionary = {}
var _scene_path: String = ""
var _temp_scene: String = ""
var _unsaved: PackedStringArray = PackedStringArray()
var _selected: Node3D = null
var _opts: Dictionary = {}
var _prep: Dictionary = {}


func setup(project_root: String = "") -> void:
	root = project_root if project_root != "" else ProjectSettings.globalize_path("res://").simplify_path()
	_cmd = Commands.new(self, root)
	dialog = Dialog.new()
	dialog.preview_requested.connect(func(o: Dictionary) -> void: preview(o))
	dialog.commit_requested.connect(func() -> void: commit())
	dialog.new_asset_requested.connect(func() -> void: publish_as_new())
	add_child(dialog)


## Opens the form for the edited scene. `base` = the selected library asset ({"display_name", "asset_id",
## "current_version_id"}) or {} when the user is publishing a new asset.
func start(library: String, base: Dictionary) -> void:
	var edited: Node = EditorInterface.get_edited_scene_root()
	if edited == null:
		message.emit("Open the scene to publish first")
		return
	_scene_path = edited.scene_file_path
	_unsaved = EditorInterface.get_unsaved_scenes()
	if _scene_path == "" or _unsaved.has(_scene_path):
		message.emit("Save the scene before publishing: only saved files are collected%s" % (
				" (unsaved: %s)" % ", ".join(_unsaved) if not _unsaved.is_empty() else ""))
		return
	_library = library
	_base = base
	_selected = _selection_node(edited)
	dialog.open_form({"name": _scene_path.get_file().get_basename(), "asset": base, "selection": _selected != null})


func _selection_node(edited: Node) -> Node3D:
	var nodes: Array[Node] = EditorInterface.get_selection().get_selected_nodes()
	if nodes.size() == 1 and nodes[0] != edited and nodes[0] is Node3D and nodes[0].scene_file_path == "":
		return nodes[0]
	return null


## Builds and previews with the form's values; shows the review (or the reason) in the dialog.
func preview(form: Dictionary, as_new: bool = false, scene_override: String = "") -> RefCounted:
	_cmd.release()
	var scene: String = scene_override if scene_override != "" else _scene_path
	if scene_override == "" and form.get("selection", false) and _selected != null:
		scene = _selection_scene(_selected)
		if scene == "":
			return _fail(Result.fail("invalid_request", "cannot save the selected subtree as a scene"))
	_opts = {"scene": scene, "library": _library, "name": form["name"] if form["name"] != "" else scene.get_file().get_basename(),
			"tags": form.get("tags", ""), "licence": form.get("licence", "") if form.get("licence", "") != "" else "unknown",
			"unsaved": _unsaved}
	if form.get("category", "") != "":
		_opts["category"] = form["category"]
	if form.get("new_version", false) and not as_new and not _base.is_empty():
		_opts["new-version-of"] = _base["asset_id"]
		_opts["expected-current"] = _base["current_version_id"]
	dialog.show_progress("Building the package and the portable GLB, then uploading the preview...")
	var r: RefCounted = await PublishCommand.prepare(_cmd, _opts)
	if not r.ok:
		return _fail(r)
	_prep = r.value
	if _prep["done"]:
		dialog.hide()
		message.emit("Already published: %s" % JSON.stringify(_prep["review"]["outcome"]))
		return r
	dialog.show_review(_prep["review"])
	return r


## Explicit commit of the reviewed preview.
func commit() -> RefCounted:
	if _prep.is_empty():
		return Result.fail("invalid_request", "nothing to commit: build a preview first")
	dialog.show_progress("Committing...")
	var r: RefCounted = await PublishCommand.commit(_cmd, _prep)
	if not r.ok:
		return _fail(r)
	dialog.hide()
	message.emit("Published version %s of asset %s" % [r.value["outcome"]["version_id"], r.value["outcome"]["asset_id"]])
	_finish()
	return r


## After a stale_pointer conflict: preview the same scene as a NEW asset (still needs its own explicit Commit).
func publish_as_new() -> RefCounted:
	var form: Dictionary = {"name": _opts.get("name", ""), "tags": _opts.get("tags", ""), "licence": _opts.get("licence", ""),
			"category": _opts.get("category", ""), "selection": false, "new_version": false}
	return await preview(form, true, str(_opts.get("scene", _scene_path)))


func _fail(r: RefCounted) -> RefCounted:
	var text: String = r.describe()
	for p: Variant in r.details.get("problems", []):
		text += "\n  - %s" % str(p)
	if r.code == "stale_pointer":
		text = "Conflict: the asset changed since it was read (current version %s).\nThe local source is untouched. Review the new version, or publish this scene as a new asset." % r.details.get("current_version_id", "unknown")
	dialog.show_error(text, r.details if r.code == "stale_pointer" else {})
	message.emit(r.describe())
	return r


func _finish() -> void:
	_cmd.release()
	if _temp_scene != "":
		DirAccess.remove_absolute(Fs.res_to_abs(root, _temp_scene))
		_temp_scene = ""
	_prep = {}


# --- selection ---------------------------------------------------------------------------------------------------

## Saves a DUPLICATE of the selected subtree as a temporary scene and returns its res:// path ("" on failure).
func _selection_scene(node: Node3D) -> String:
	var dup: Node3D = node.duplicate(Node.DUPLICATE_USE_INSTANTIATION) as Node3D
	if dup == null:
		return ""
	dup.scene_file_path = ""
	_own_children(dup, dup)
	var packed := PackedScene.new()
	var path: String = "res://.assetstudio/publish/selection_%s.tscn" % Graph.slugify(String(node.name), "node")
	DirAccess.make_dir_recursive_absolute(Fs.res_to_abs(root, path).get_base_dir())
	var ok: bool = packed.pack(dup) == OK and ResourceSaver.save(packed, path) == OK
	dup.free()
	if not ok:
		return ""
	_temp_scene = path
	return path


static func _own_children(n: Node, owner_root: Node) -> void:
	for c: Node in n.get_children():
		c.owner = owner_root
		if c.scene_file_path == "":
			_own_children(c, owner_root)
