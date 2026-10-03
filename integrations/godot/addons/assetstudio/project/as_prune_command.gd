@tool
extends RefCounted
# `prune-deliveries [--dry-run | --apply]`: explicit, lock-aware removal of orphaned managed delivery directories
# <managed_root>/<asset_key>/<manifest_sha256>/ (spec §7). A directory is kept when ANY lock dependency still lists
# that (asset_key, manifest_sha256) as a delivery (a superset of "reached by a binding or a scene_binding /
# world_generation root closure"). Default is a dry run. Deletion is one coordinator transaction, so it is crash-safe
# and rolls back as a whole; opening it takes the mutex and recovers any pending transaction first, so none can still
# reference a directory. The raw blob cache is never touched (that is `cache-prune`). Only directories named by two
# sha256 levels are candidates: unknown entries and `.staging` are left alone.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")
const Coordinator = preload("res://addons/assetstudio/project/as_mutation_coordinator.gd")


static func run(cmd: RefCounted, o: Dictionary) -> int:
	var apply: bool = o.has("apply")
	var txn: RefCounted = null
	if apply:
		txn = cmd.open_txn("prune-deliveries")
		if txn == null:
			return 1
	elif has_pending_txn(cmd.root):
		cmd.err("a mutation transaction is pending; run `verify --locked --offline` to recover it first")
		return 1
	var loaded: RefCounted = cmd.load_locked()
	if not loaded.ok:
		if txn != null:
			txn.close()
		return cmd.fail_exit(loaded)
	var found: Dictionary = plan(cmd.root, loaded.value["config"].call("managed_rel"), loaded.value["lock"])
	for rel: String in found["orphans"]:
		cmd.say("%s %s" % ["remove" if apply else "would remove", rel])
	if not apply:
		cmd.say("dry run: %d orphaned delivery(ies); pass --apply to delete" % (found["orphans"] as Array).size())
		return 0
	return _apply(cmd, txn, found)


static func _apply(cmd: RefCounted, txn: RefCounted, found: Dictionary) -> int:
	if (found["orphans"] as Array).is_empty():
		txn.close()
		cmd.say("pruned 0 delivery(ies)")
		return 0
	for rel: String in found["deletes"]:
		txn.add_delete(rel)
	var r: RefCounted = txn.commit()
	txn.close()
	if not r.ok:
		return cmd.fail_exit(r)
	cmd.say("pruned %d delivery(ies)" % (found["orphans"] as Array).size())
	return 0


static func has_pending_txn(root: String) -> bool:
	return not (Fs.list_dir(root.path_join(Coordinator.META_DIR).path_join("txn"))["dirs"] as PackedStringArray).is_empty()


## {"orphans": [rel of each unreferenced delivery dir], "deletes": [rel to delete: the delivery dirs, or their
## <asset_key> dir when nothing else is left in it]}.
static func plan(root: String, managed_rel: String, lock: RefCounted) -> Dictionary:
	var keep: Dictionary = _referenced(lock)
	var orphans: Array = []
	var deletes: Array = []
	for key: String in Fs.list_dir(root.path_join(managed_rel))["dirs"]:
		if not Schema.matches("sha256", key):
			continue
		var listing: Dictionary = Fs.list_dir(root.path_join(managed_rel).path_join(key))
		var drop: Array = []
		for msha: String in listing["dirs"]:
			if Schema.matches("sha256", msha) and not keep.has("%s/%s" % [key, msha]):
				drop.append(msha)
		for msha: String in drop:
			orphans.append(managed_rel.path_join(key).path_join(msha))
		if not drop.is_empty() and (listing["files"] as PackedStringArray).is_empty() \
				and drop.size() == (listing["dirs"] as PackedStringArray).size():
			deletes.append(managed_rel.path_join(key))
		else:
			for msha: String in drop:
				deletes.append(managed_rel.path_join(key).path_join(msha))
	return {"orphans": orphans, "deletes": deletes}


static func _referenced(lock: RefCounted) -> Dictionary:
	var keep: Dictionary = {}
	for key: String in lock.call("dependencies"):
		for rep: String in lock.call("dependencies")[key]["deliveries"]:
			keep["%s/%s" % [key, lock.call("dependencies")[key]["deliveries"][rep]["manifest_sha256"]]] = true
	return keep
