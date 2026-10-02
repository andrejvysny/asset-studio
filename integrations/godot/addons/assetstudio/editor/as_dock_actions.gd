@tool
extends Node
# Editor-side orchestration for the dock: install, place, update review/apply, restore previous version, update
# detection. Every disk change goes through the same project/ functions the CLI uses (AddCommand, Finalize,
# UpdateCommand); this class only adds what needs the editor: waiting for the import, the edited scene, the viewport
# and EditorUndoRedoManager (via as_place.gd).

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const AssetRef = preload("res://addons/assetstudio/core/as_asset_ref.gd")
const Registry = preload("res://addons/assetstudio/core/as_connection_registry.gd")
const Client = preload("res://addons/assetstudio/core/as_library_client.gd")
const Version = preload("res://addons/assetstudio/core/as_version.gd")
const Config = preload("res://addons/assetstudio/project/as_project_config.gd")
const Lock = preload("res://addons/assetstudio/project/as_project_lock.gd")
const Installer = preload("res://addons/assetstudio/project/as_installer.gd")
const State = preload("res://addons/assetstudio/project/as_project_state.gd")
const Commands = preload("res://addons/assetstudio/project/as_commands.gd")
const AddCommand = preload("res://addons/assetstudio/project/as_add_command.gd")
const UpdateCommand = preload("res://addons/assetstudio/project/as_update_command.gd")
const Finalize = preload("res://addons/assetstudio/project/as_finalize.gd")
const BindingState = preload("res://addons/assetstudio/project/as_binding_state.gd")
const DescriptorDiff = preload("res://addons/assetstudio/project/as_descriptor_diff.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")
const Place = preload("res://addons/assetstudio/editor/as_place.gd")

## The dock re-renders on this; `message` is a one-line status or error for the user.
signal changed
signal message(text: String)

var root: String = ""
var config: RefCounted = null
var lock: RefCounted = null
## {binding_id: BindingState info}, recomputed by reload().
var infos: Dictionary = {}
## {asset_id: BindingState.DOWNLOADING | PREPARING | UNSUPPORTED} for work in flight.
var transient: Dictionary = {}
var client: Node = null
var import_wait_s: float = 120.0

var _plugin: EditorPlugin = null
var _cmd: RefCounted = null
var _updates: Dictionary = {}  # binding_id -> target version_id (offered)
var _dismissed: Dictionary = {}  # binding_id -> target version_id the user declined


func setup(plugin: EditorPlugin, project_root: String = "") -> void:
	_plugin = plugin
	root = project_root if project_root != "" else ProjectSettings.globalize_path("res://").simplify_path()
	_cmd = Commands.new(self, root)
	reload()


## Re-reads config, lock and state. Safe without any project file or connection.
func reload() -> void:
	config = null
	lock = null
	var cfg: RefCounted = Config.load_from(root)
	if cfg.ok:
		config = cfg.value
		lock = Lock.new_empty(Version.VERSION, Installer.INSTALLER_VERSION)
		var path: String = root.path_join(Lock.FILE_NAME)
		if FileAccess.file_exists(path):
			var parsed: RefCounted = Lock.parse_bytes(Fs.read_bytes(path))
			if parsed.ok:
				lock = parsed.value
			else:
				message.emit("lock file is invalid: %s" % parsed.message)
	infos = {}
	if lock != null:
		infos = BindingState.binding_infos(root, config, lock, State.read_state(root), _updates)
	changed.emit()


## {"connected": bool, "text": String}.
func connection_status() -> Dictionary:
	if config == null:
		return {"connected": false, "text": "No assetstudio.project.json: run the `connect` command first"}
	var reg: RefCounted = Registry.new()
	var sid: String = config.get("server_id")
	if reg.get_connection(sid) == null or not reg.has_credential(sid):
		return {"connected": false, "text": "Not connected to server %s (run `connect`)" % sid.left(8)}
	return {"connected": true, "text": "Server %s" % sid.left(8)}


## Creates the (single) client when a connection is configured; null otherwise.
func ensure_client() -> Node:
	if client != null or not connection_status()["connected"]:
		return client
	client = Client.new()
	client.setup(Registry.new(), config.get("server_id"))
	add_child(client)
	return client


func bindings_of(asset_id: String, library_id: String) -> Array:
	return infos.keys().filter(func(b: String) -> bool:
		return infos[b]["asset_id"] == asset_id and infos[b]["library_id"] == library_id)


# --- install -----------------------------------------------------------------------------------------------------

## Install = add flow, then wait for the import, then finalize (slot resolution + material policy + wrapper).
func install(item: Dictionary) -> RefCounted:
	if ensure_client() == null:
		return _say(Result.fail("unauthorized", "not connected"))
	var aid: String = item["asset_id"]
	_set_transient(aid, BindingState.DOWNLOADING)
	var added: RefCounted = await AddCommand.execute(_cmd, {"library": item["library_id"], "asset": aid,
			"version": item["current_version_id"]})
	_cmd.release()
	if not added.ok:
		_set_transient(aid, BindingState.UNSUPPORTED if added.code == "unsupported_representation" else "")
		return _say(added)
	_set_transient(aid, BindingState.PREPARING)
	var done: RefCounted = await import_and_finalize(added.value["binding_id"])
	_set_transient(aid, "")
	return _say(done)


## Waits (bounded) until the editor has imported the binding's GLB, then finalizes the binding.
func import_and_finalize(binding_id: String) -> RefCounted:
	reload()
	var glb: String = _glb_res_path(binding_id)
	if glb == "":
		return Result.fail("invalid_request", "binding %s has no installed GLB" % binding_id)
	var fs: EditorFileSystem = EditorInterface.get_resource_filesystem()
	fs.scan()
	var waited: float = 0.0
	while waited < import_wait_s and (fs.is_scanning() or not ResourceLoader.exists(glb)):
		await get_tree().create_timer(0.25).timeout
		waited += 0.25
	if not ResourceLoader.exists(glb):
		return Result.fail("temporarily_unavailable", "timed out waiting for the editor to import %s" % glb)
	var r: RefCounted = Finalize.finalize_bindings(_cmd, [binding_id], false)
	_cmd.release()
	if r.ok and not (r.value["failed"] as Array).is_empty():
		var f: Dictionary = r.value["failed"][0]
		r = Result.fail(f["code"], f["message"])
	fs.scan()
	reload()
	return r


func _glb_res_path(binding_id: String) -> String:
	if lock == null or not lock.bindings().has(binding_id):
		return ""
	var rel: String = Finalize.delivery_rel(config, lock, binding_id)
	for f: String in Fs.list_dir(root.path_join(rel))["files"]:
		if f.get_extension().to_lower() == "glb":
			return "res://%s/%s" % [rel, f]
	return ""


# --- place -------------------------------------------------------------------------------------------------------

## Instantiates the wrapper of a Ready binding in the edited scene through EditorUndoRedoManager.
func place(binding_id: String) -> Node:
	var info: Dictionary = infos.get(binding_id, {})
	if info.is_empty() or not BindingState.is_placeable(info["state"]):
		message.emit("Only Ready assets can be placed")
		return null
	var scene_root: Node = EditorInterface.get_edited_scene_root()
	if scene_root == null:
		message.emit("Open a scene first")
		return null
	var packed: PackedScene = ResourceLoader.load(info["wrapper_res"], "", ResourceLoader.CACHE_MODE_REPLACE) as PackedScene
	if packed == null:
		message.emit("Cannot load %s" % info["wrapper_res"])
		return null
	return Place.place(_plugin.get_undo_redo(), scene_root, _selected_parent(scene_root), packed,
			_viewport_point(scene_root))


func _selected_parent(scene_root: Node) -> Node:
	for n: Node in EditorInterface.get_selection().get_selected_nodes():
		if n is Node3D and (n == scene_root or scene_root.is_ancestor_of(n)):
			return n
	return scene_root


## Hit of the 3D viewport's centre ray against physics bodies of the edited scene, else the origin.
func _viewport_point(scene_root: Node) -> Vector3:
	var vp: SubViewport = EditorInterface.get_editor_viewport_3d(0)
	var cam: Camera3D = vp.get_camera_3d() if vp != null else null
	if cam == null or not scene_root is Node3D:
		return Vector3.ZERO
	var centre: Vector2 = Vector2(vp.size) * 0.5
	var from: Vector3 = cam.project_ray_origin(centre)
	var space: PhysicsDirectSpaceState3D = (scene_root as Node3D).get_world_3d().direct_space_state
	var hit: Dictionary = space.intersect_ray(PhysicsRayQueryParameters3D.create(from, from + cam.project_ray_normal(centre) * 1000.0))
	return hit["position"] if not hit.is_empty() else Vector3.ZERO


# --- update detection and review ---------------------------------------------------------------------------------

## Resolves "latest" once per asset into an exact version and marks bindings that are behind it.
## `only_asset` limits the check to one asset (change events).
func check_updates(only_asset: String = "") -> void:
	if ensure_client() == null or lock == null:
		return
	var seen: Dictionary = {}
	for bid: String in infos:
		var i: Dictionary = infos[bid]
		var k: String = "%s/%s" % [i["library_id"], i["asset_id"]]
		if seen.has(k) or (only_asset != "" and i["asset_id"] != only_asset):
			continue
		var detail: RefCounted = await client.asset(i["library_id"], i["asset_id"])
		seen[k] = true
		if detail.ok:
			_offer(i["library_id"], i["asset_id"], str(detail.value.get("current_version_id", "")))
	reload()


func _offer(library_id: String, asset_id: String, current: String) -> void:
	for bid: String in bindings_of(asset_id, library_id):
		if infos[bid]["version_id"] == current or current == "":
			_updates.erase(bid)
		elif _dismissed.get(bid) != current:
			_updates[bid] = current


## A server change for the asset withdraws earlier "dismissed" decisions (design §8).
func on_asset_current_changed(library_id: String, asset_id: String) -> void:
	for bid: String in bindings_of(asset_id, library_id):
		_dismissed.erase(bid)
	check_updates(asset_id)


func dismiss(binding_id: String) -> void:
	_dismissed[binding_id] = _updates.get(binding_id, "")
	_updates.erase(binding_id)
	reload()


## value = {"binding_id", "from", "to", "diff": PackedStringArray, "instances": int}.
func review(binding_id: String) -> RefCounted:
	var info: Dictionary = infos.get(binding_id, {})
	if info.is_empty() or info["target_version"] == "" or ensure_client() == null:
		return Result.fail("invalid_request", "no update is offered for %s" % binding_id)
	var dep: Dictionary = lock.dependencies()[info["asset_key"]]
	var old_d: RefCounted = Finalize.load_descriptor(_cmd.cache, dep)
	if not old_d.ok:
		return old_d
	var ref: Dictionary = (dep["asset_ref"] as Dictionary).duplicate()
	ref["version_id"] = info["target_version"]
	var new_d: RefCounted = await DescriptorDiff.fetch_descriptor(client, AssetRef.parse(ref).value)
	if not new_d.ok:
		return new_d
	var scene_root: Node = EditorInterface.get_edited_scene_root()
	var count: int = Place.instances_of(_scene_nodes(scene_root), binding_id).size() if scene_root != null else 0
	return Result.success({"binding_id": binding_id, "from": info["version_id"], "to": info["target_version"],
			"diff": DescriptorDiff.diff(old_d.value.data, new_d.value.data), "instances": count})


func _scene_nodes(n: Node) -> Array:
	var out: Array = [n]
	for c: Node in n.get_children():
		out.append_array(_scene_nodes(c))
	return out


## "Update binding": every instance follows (the wrapper is rewritten in place).
func apply_update_binding(binding_id: String) -> RefCounted:
	var target: String = infos.get(binding_id, {}).get("target_version", "")
	if target == "":
		return _say(Result.fail("invalid_request", "no update is offered for %s" % binding_id))
	var r: RefCounted = await UpdateCommand.update(_cmd, binding_id, target, "")
	_cmd.release()
	if r.ok:
		_updates.erase(binding_id)
		r = await import_and_finalize(binding_id)
	return _say(r)


## "Update selected instances": a new binding for the target version; the selected instances of the old binding
## are swapped in the edited scene (Undo restores them), other instances keep the old binding.
func apply_update_instances(binding_id: String) -> RefCounted:
	var info: Dictionary = infos.get(binding_id, {})
	var scene_root: Node = EditorInterface.get_edited_scene_root()
	var picked: Array = Place.instances_of(EditorInterface.get_selection().get_selected_nodes(), binding_id)
	if info.is_empty() or info["target_version"] == "" or scene_root == null or picked.is_empty():
		return _say(Result.fail("invalid_request", "select instances of %s in the open scene first" % binding_id))
	var new_id: String = _new_binding_id(binding_id, info)
	var r: RefCounted = await UpdateCommand.update(_cmd, binding_id, info["target_version"], new_id)
	_cmd.release()
	if r.ok:
		r = await import_and_finalize(new_id)
	if r.ok:
		var packed: PackedScene = ResourceLoader.load(infos[new_id]["wrapper_res"], "", ResourceLoader.CACHE_MODE_REPLACE) as PackedScene
		Place.swap_instances(_plugin.get_undo_redo(), scene_root, picked, packed)
	return _say(r)


func _new_binding_id(binding_id: String, info: Dictionary) -> String:
	var ref: Dictionary = (lock.dependencies()[info["asset_key"]]["asset_ref"] as Dictionary).duplicate()
	ref["version_id"] = info["target_version"]
	var key: String = AssetRef.parse(ref).value.call("key")
	var base: String = RegEx.create_from_string("-[0-9a-f]{8}(-[0-9]+)?$").sub(binding_id, "")
	return lock.unique_binding_id(base, key)


## "Restore previous version" (history.json): re-points the binding to the version it had before.
func restore_previous(binding_id: String) -> RefCounted:
	var r: RefCounted = await UpdateCommand.rollback(_cmd, binding_id)
	_cmd.release()
	if r.ok:
		r = await import_and_finalize(binding_id)
	return _say(r)


func can_restore_previous(binding_id: String) -> bool:
	return infos.has(binding_id) and not UpdateCommand.last_move(root, binding_id, infos[binding_id]["asset_key"]).is_empty()


func _set_transient(asset_id: String, state: String) -> void:
	if state == "":
		transient.erase(asset_id)
	else:
		transient[asset_id] = state
	changed.emit()


func _say(r: RefCounted) -> RefCounted:
	message.emit("Done" if r.ok else r.describe())
	return r
