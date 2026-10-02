@tool
extends RefCounted
# Addon-private bookkeeping under <project>/.assetstudio/ (git-ignored): state.json (bindings awaiting the
# post-import `finalize`) and wrappers.json (hash of each wrapper as written, for AS-08 conflict detection).
# Both are plain canonical JSON and are written through the mutation coordinator, never directly.

const CJson = preload("res://addons/assetstudio/core/as_canonical_json.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")

const STATE_REL: String = ".assetstudio/state.json"
const WRAPPERS_REL: String = ".assetstudio/wrappers.json"


static func read_state(root: String) -> Dictionary:
	var d: Dictionary = _read(root.path_join(STATE_REL))
	if not d.get("pending_import") is Array:
		d["pending_import"] = []
	d["schema_version"] = 1
	return d


static func state_bytes(state: Dictionary) -> PackedByteArray:
	var pending: Array = (state["pending_import"] as Array).duplicate()
	pending.sort()
	return CJson.encode({"schema_version": 1, "pending_import": pending}).value


static func wrappers_bytes(root: String, binding_id: String, path: String, sha256: String) -> PackedByteArray:
	var d: Dictionary = _read(root.path_join(WRAPPERS_REL))
	var wrappers: Dictionary = d["wrappers"] if d.get("wrappers") is Dictionary else {}
	wrappers[binding_id] = {"path": path, "sha256": sha256}
	return CJson.encode({"schema_version": 1, "wrappers": wrappers}).value


static func _read(path: String) -> Dictionary:
	var p: RefCounted = CJson.parse_canonical(Fs.read_bytes(path))
	return (p.value as Dictionary).duplicate(true) if p.ok and p.value is Dictionary else {}


## {"path", "sha256"} recorded when the addon wrote the wrapper of `binding_id`, or {}.
static func wrapper_record(root: String, binding_id: String) -> Dictionary:
	var d: Dictionary = _read(root.path_join(WRAPPERS_REL))
	var wrappers: Dictionary = d["wrappers"] if d.get("wrappers") is Dictionary else {}
	return (wrappers[binding_id] as Dictionary).duplicate() if wrappers.get(binding_id) is Dictionary else {}


## Like wrappers_bytes for several bindings at once: updates = {binding_id: {"path", "sha256"}}.
static func wrappers_bytes_multi(root: String, updates: Dictionary) -> PackedByteArray:
	var d: Dictionary = _read(root.path_join(WRAPPERS_REL))
	var wrappers: Dictionary = d["wrappers"] if d.get("wrappers") is Dictionary else {}
	for id: String in updates:
		wrappers[id] = updates[id]
	return CJson.encode({"schema_version": 1, "wrappers": wrappers}).value
