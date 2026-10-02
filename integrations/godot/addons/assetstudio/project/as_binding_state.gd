@tool
extends RefCounted
# Display state of bindings and of server list items (design §8): Remote, Downloading, Preparing, Ready,
# Update available, Unavailable, Unsupported. Pure functions over the lock and .assetstudio/state.json; the dock only
# renders what is computed here. "Ready" and "Update available" bindings can be placed (the installed version
# works); the badge only tells that a newer exact version exists.

const Fs = preload("res://addons/assetstudio/project/as_fs.gd")
const Wrapper = preload("res://addons/assetstudio/project/as_wrapper.gd")
const Finalize = preload("res://addons/assetstudio/project/as_finalize.gd")

const REMOTE: String = "remote"
const DOWNLOADING: String = "downloading"
const PREPARING: String = "preparing"
const READY: String = "ready"
const UPDATE: String = "update_available"
const UNAVAILABLE: String = "unavailable"
const UNSUPPORTED: String = "unsupported"


## `updates` = {binding_id: target_version_id} (dismissed ones are not in it).
## Returns {binding_id: {"state", "wrapper_res", "wrapper_rel", "library_id", "asset_id", "version_id", "asset_key"}}.
static func binding_infos(root: String, config: RefCounted, lock: RefCounted, state: Dictionary,
		updates: Dictionary) -> Dictionary:
	var out: Dictionary = {}
	for bid: String in lock.bindings():
		var b: Dictionary = lock.bindings()[bid]
		var ref: Dictionary = lock.dependencies()[b["asset_key"]]["asset_ref"]
		var rel: String = Wrapper.wrapper_rel(config.call("prefab_rel"), bid)
		var st: String = _state_of(root, config, lock, state, bid, rel)
		if updates.has(bid) and st == READY:
			st = UPDATE
		out[bid] = {"state": st, "wrapper_rel": rel, "wrapper_res": "res://" + rel,
				"library_id": ref["library_id"], "asset_id": ref["asset_id"], "version_id": ref["version_id"],
				"asset_key": b["asset_key"], "target_version": updates.get(bid, "")}
	return out


static func is_placeable(st: String) -> bool:
	return st == READY or st == UPDATE


static func _state_of(root: String, config: RefCounted, lock: RefCounted, state: Dictionary, bid: String,
		rel: String) -> String:
	if (state["pending_import"] as Array).has(bid):
		return PREPARING
	if not FileAccess.file_exists(root.path_join(rel)):
		return UNAVAILABLE
	var dir: String = root.path_join(Finalize.delivery_rel(config, lock, bid))
	if not FileAccess.file_exists(dir.path_join("receipt.json")):
		return UNAVAILABLE
	for f: String in Fs.list_dir(dir)["files"]:
		if f.get_extension().to_lower() == "glb" and not FileAccess.file_exists(dir.path_join(f) + ".import"):
			return PREPARING
	return READY


## State of a server list item {asset_id, current_version_id, library_id} given the binding infos.
## `transient` = {asset_id: DOWNLOADING | PREPARING | UNSUPPORTED} for work in flight in the dock.
static func item_state(item: Dictionary, infos: Dictionary, transient: Dictionary) -> String:
	if transient.has(item["asset_id"]):
		return transient[item["asset_id"]]
	var best: String = REMOTE
	for bid: String in infos:
		var i: Dictionary = infos[bid]
		if i["asset_id"] != item["asset_id"] or i["library_id"] != item["library_id"]:
			continue
		if i["version_id"] == item["current_version_id"]:
			return i["state"]
		best = UPDATE
	return best
