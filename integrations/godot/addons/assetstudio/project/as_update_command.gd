@tool
extends RefCounted
# `update` and `rollback` (design §8, §9). Both go through one coordinator transaction and, like `add`, write the
# plain wrapper and mark the binding pending_import: the new GLB is not imported yet, so `finalize` applies the
# material policy afterwards. Managed delivery directories are never deleted (rollback re-uses them offline).
#
#   update   --binding B --version V                  re-point B (all its instances) to version V
#   update   --binding B --version V --new-binding N  bind V as a NEW binding N (own wrapper); B is untouched
#   rollback --binding B                              undo the last update/rollback of B (history.json summary)
#
# Binding re-pointing records {kind, binding_id, from_key, to_key, mode:"rewrite", from_deps} in the transaction
# summary: from_deps are the lock dependencies that were pruned, so rollback can restore the lock exactly.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")
const AssetRef = preload("res://addons/assetstudio/core/as_asset_ref.gd")
const CJson = preload("res://addons/assetstudio/core/as_canonical_json.gd")
const Lock = preload("res://addons/assetstudio/project/as_project_lock.gd")
const Installer = preload("res://addons/assetstudio/project/as_installer.gd")
const Restore = preload("res://addons/assetstudio/project/as_restore.gd")
const Wrapper = preload("res://addons/assetstudio/project/as_wrapper.gd")
const State = preload("res://addons/assetstudio/project/as_project_state.gd")
const AddCommand = preload("res://addons/assetstudio/project/as_add_command.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")

const REPRESENTATION: String = "portable_glb_v1"


static func run_update(cmd: RefCounted, o: Dictionary) -> int:
	var r: RefCounted = await update(cmd, o["binding"], o["version"], o.get("new-binding", ""))
	if r.ok:
		return 0
	if r.details.get("usage", false):
		cmd.err(r.message)
		return 2
	return cmd.fail_exit(r)


static func run_rollback(cmd: RefCounted, o: Dictionary) -> int:
	var r: RefCounted = await rollback(cmd, o["binding"])
	return 0 if r.ok else cmd.fail_exit(r)


## `new_binding` empty = rewrite `binding_id`. value = {"binding_id": the binding that now holds the target}.
static func update(cmd: RefCounted, binding_id: String, version_id: String, new_binding: String) -> RefCounted:
	var loaded: RefCounted = cmd.load_locked()
	if not loaded.ok:
		return loaded
	var lock: RefCounted = loaded.value["lock"]
	var config: RefCounted = loaded.value["config"]
	if not lock.bindings().has(binding_id):
		return Result.fail("invalid_request", "unknown binding %s" % binding_id, false, {"usage": true})
	if new_binding != "" and (not Schema.matches("slug", new_binding) or lock.bindings().has(new_binding)):
		return Result.fail("invalid_request", "--new-binding must be an unused slug: 1-64 chars of a-z 0-9 _ . - starting with a letter or digit", false, {"usage": true})
	var old_key: String = lock.bindings()[binding_id]["asset_key"]
	var ref: RefCounted = _target_ref(lock.dependencies()[old_key]["asset_ref"], version_id)
	if not ref.ok:
		return Result.fail("invalid_request", ref.message, false, {"usage": true})
	if ref.value.call("key") == old_key:
		return Result.fail("invalid_request", "%s is already at %s" % [binding_id, version_id])
	var c: RefCounted = cmd.open_txn("update")
	if c == null:
		return Result.fail(Result.CODE_LOCKED, "cannot open a transaction")
	var nodes: RefCounted = await AddCommand.resolve_closure(cmd.make_resolver(config.get("server_id"), false), ref.value)
	var r: RefCounted = nodes
	if nodes.ok:
		r = _apply_update(c, config, lock, nodes.value, binding_id, new_binding)
	if r.ok:
		r = c.commit()
	c.close()
	if not r.ok:
		return r
	cmd.pin_lock(lock)
	var holder: String = new_binding if new_binding != "" else binding_id
	cmd.say("updated %s to %s: run the headless import, then `finalize`" % [holder, version_id])
	return Result.success({"binding_id": holder})


static func _target_ref(old_ref: Dictionary, version_id: String) -> RefCounted:
	var d: Dictionary = old_ref.duplicate()
	d["version_id"] = version_id
	return AssetRef.parse(d)


static func _apply_update(c: RefCounted, config: RefCounted, lock: RefCounted, nodes: Array, binding_id: String,
		new_binding: String) -> RefCounted:
	var new_key: String = nodes[0]["ref"].call("key")
	if new_binding != "":
		var policy: Dictionary = lock.bindings()[binding_id]["material_policy"].duplicate()
		c.summary = {"kind": "update", "mode": "new_binding", "binding_id": binding_id,
				"new_binding": new_binding, "to_key": new_key}
		return AddCommand.apply_binding(c, config, lock, nodes, new_binding, policy)
	var old_key: String = lock.bindings()[binding_id]["asset_key"]
	var before: Dictionary = (lock.dependencies() as Dictionary).duplicate(true)
	var inst: RefCounted = AddCommand.install_nodes(c, config, lock, nodes)
	if not inst.ok:
		return inst
	var moved: RefCounted = _repoint(lock, binding_id, new_key)
	if not moved.ok:
		return moved
	c.summary = _summary("update", binding_id, old_key, new_key, _pruned(before, lock))
	return _queue_wrapper(c, config, lock, nodes[0], binding_id)


## Re-points the binding and its root to `key`, then prunes dependencies nothing references any more.
static func _repoint(lock: RefCounted, binding_id: String, key: String) -> RefCounted:
	lock.bindings()[binding_id]["asset_key"] = key
	lock.add_root("scene_binding", binding_id, lock.closure(key))
	lock.prune_unreferenced()
	return Result.success()


static func _pruned(before: Dictionary, lock: RefCounted) -> Dictionary:
	var out: Dictionary = {}
	for k: String in before:
		if not lock.dependencies().has(k):
			out[k] = before[k]
	return out


static func _summary(kind: String, binding_id: String, from_key: String, to_key: String, from_deps: Dictionary) -> Dictionary:
	return {"kind": kind, "mode": "rewrite", "binding_id": binding_id, "from_key": from_key, "to_key": to_key,
			"from_deps": from_deps}


## Writes lock, plain wrapper, wrappers.json and the pending mark for a re-pointed binding.
static func _queue_wrapper(c: RefCounted, config: RefCounted, lock: RefCounted, root_node: Dictionary,
		binding_id: String) -> RefCounted:
	var rel: String = Wrapper.wrapper_rel(config.call("prefab_rel"), binding_id)
	var conflict: String = Wrapper.check_conflict(c.root, binding_id, rel)
	if conflict != "":
		return Result.fail("conflict", conflict)
	var bytes: RefCounted = lock.to_bytes()
	if not bytes.ok:
		return bytes
	c.add_write(Lock.FILE_NAME, bytes.value)
	var text: PackedByteArray = AddCommand.wrapper_text(config, root_node, binding_id)
	c.add_write(rel, text)
	c.add_write(State.WRAPPERS_REL, State.wrappers_bytes(c.root, binding_id, rel, Fs.sha256_bytes(text)))
	var state: Dictionary = State.read_state(c.root)
	if not (state["pending_import"] as Array).has(binding_id):
		(state["pending_import"] as Array).append(binding_id)
	c.add_write(State.STATE_REL, State.state_bytes(state))
	return Result.success()


# --- rollback ------------------------------------------------------------------------------------------------

## The newest history summary that moved `binding_id` onto its current key, or {}.
static func last_move(root: String, binding_id: String, current_key: String) -> Dictionary:
	var parsed: RefCounted = CJson.parse_canonical(Fs.read_bytes(root.path_join(".assetstudio/history.json")))
	if not parsed.ok or not (parsed.value as Dictionary).get("entries") is Array:
		return {}
	var entries: Array = parsed.value["entries"]
	for i: int in entries.size():
		var s: Variant = (entries[entries.size() - 1 - i] as Dictionary).get("summary")
		if s is Dictionary and s.get("binding_id") == binding_id and s.get("mode") == "rewrite" \
				and s.get("kind") in ["update", "rollback"]:
			return s if s.get("to_key") == current_key else {}
	return {}


static func rollback(cmd: RefCounted, binding_id: String) -> RefCounted:
	var loaded: RefCounted = cmd.load_locked()
	if not loaded.ok:
		return loaded
	var lock: RefCounted = loaded.value["lock"]
	var config: RefCounted = loaded.value["config"]
	if not lock.bindings().has(binding_id):
		return Result.fail("invalid_request", "unknown binding %s" % binding_id)
	var cur_key: String = lock.bindings()[binding_id]["asset_key"]
	var move: Dictionary = last_move(cmd.root, binding_id, cur_key)
	if move.is_empty():
		return Result.fail("invalid_request", "no previous version recorded for %s" % binding_id)
	var c: RefCounted = cmd.open_txn("rollback")
	if c == null:
		return Result.fail(Result.CODE_LOCKED, "cannot open a transaction")
	var r: RefCounted = await _apply_rollback(cmd, c, config, lock, binding_id, move)
	if r.ok:
		r = c.commit()
	c.close()
	if not r.ok:
		return r
	cmd.pin_lock(lock)
	cmd.say("rolled %s back to %s: run the headless import, then `finalize`" % [binding_id, str(move["from_key"]).left(8)])
	return Result.success({"binding_id": binding_id})


static func _apply_rollback(cmd: RefCounted, c: RefCounted, config: RefCounted, lock: RefCounted, binding_id: String,
		move: Dictionary) -> RefCounted:
	var before: Dictionary = (lock.dependencies() as Dictionary).duplicate(true)
	var cur_key: String = move["to_key"]
	for k: String in move["from_deps"]:
		if not lock.has_dependency(k):
			lock.dependencies()[k] = move["from_deps"][k]
	var moved: RefCounted = _repoint(lock, binding_id, move["from_key"])
	if not moved.ok:
		return moved
	var reinstalled: RefCounted = await _reinstall(cmd, c, config, lock, move["from_key"])
	if not reinstalled.ok:
		return reinstalled
	c.summary = _summary("rollback", binding_id, cur_key, move["from_key"], _pruned(before, lock))
	return _queue_wrapper(c, config, lock, reinstalled.value, binding_id)


## Makes sure every delivery of the closure of `key` is installed (cache or server, pinned to the locked
## delivery). value = the root node {"ref", "prep"} (for the wrapper).
static func _reinstall(cmd: RefCounted, c: RefCounted, config: RefCounted, lock: RefCounted, key: String) -> RefCounted:
	var resolver: Node = cmd.make_resolver(config.get("server_id"), false)
	var root_node: Dictionary = {}
	for k: String in lock.closure(key):
		var dep: Dictionary = lock.dependencies()[k]
		var prep: RefCounted = await Restore.fetch_pinned(resolver, dep, REPRESENTATION)
		if not prep.ok:
			return prep
		var ref: RefCounted = AssetRef.parse(dep["asset_ref"]).value
		var inst: RefCounted = Installer.install(c, config.call("managed_rel"), ref, prep.value)
		if not inst.ok:
			return inst
		if k == key:
			root_node = {"ref": ref, "prep": prep.value}
	return Result.success(root_node)
