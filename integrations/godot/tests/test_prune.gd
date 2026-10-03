extends "res://tests/project_test_base.gd"
# prune-deliveries: lock-aware, transactional removal of orphaned managed delivery directories.

const Commands = preload("res://addons/assetstudio/project/as_commands.gd")
const PruneCommand = preload("res://addons/assetstudio/project/as_prune_command.gd")
const Coordinator = preload("res://addons/assetstudio/project/as_mutation_coordinator.gd")
const Config = preload("res://addons/assetstudio/project/as_project_config.gd")
const Lock = preload("res://addons/assetstudio/project/as_project_lock.gd")
const Descriptor = preload("res://addons/assetstudio/core/as_asset_descriptor.gd")
const Manifest = preload("res://addons/assetstudio/core/as_delivery_manifest.gd")

const SERVER: String = "6f1c2a52-3c2e-4d4b-9a57-0b6f6f0c1d2e"
const MANAGED: String = "assets/library"
const OLD_SHA: String = "1111111111111111111111111111111111111111111111111111111111111111"
const GONE_KEY: String = "2222222222222222222222222222222222222222222222222222222222222222"
const GONE_SHA: String = "3333333333333333333333333333333333333333333333333333333333333333"


## Project with a lock binding the primitive_prop v1 delivery; returns the referenced dir (relative) and key.
func _project() -> Dictionary:
	var root: String = tmp_dir("prune")
	Fs.write_atomic(root.path_join(Config.FILE_NAME), Config.defaults(SERVER).to_bytes().value)
	var desc: RefCounted = Descriptor.parse_bytes(fixture("descriptors/valid/primitive_prop.json")).value
	var man: RefCounted = Manifest.parse_bytes(fixture("manifests/valid/portable_primitive_prop.json")).value
	var key: String = desc.asset_ref.key()
	var lock: RefCounted = Lock.new_empty("0.1.0", "1.0.0")
	var delivery: Dictionary = {"delivery_id": man.data["delivery_id"], "manifest_sha256": man.raw_sha256,
			"profile_id": man.data["profile_id"], "profile_version": man.data["profile_version"]}
	assert_eq(lock.add_dependency(desc.asset_ref, desc.raw_sha256, "portable_glb_v1", delivery, []), "", "dep")
	lock.add_binding("b-1", key, "portable_glb_v1", {"mode": "preserve", "profile_id": null, "profile_sha256": null})
	lock.add_root("scene_binding", "b-1", lock.closure(key))
	Fs.write_atomic(root.path_join(Lock.FILE_NAME), lock.to_bytes().value)
	var kept: String = MANAGED.path_join(key).path_join(man.raw_sha256)
	for rel: String in [kept, MANAGED.path_join(key).path_join(OLD_SHA), MANAGED.path_join(GONE_KEY).path_join(GONE_SHA),
			MANAGED.path_join("notes"), MANAGED.path_join(".staging/t1")]:
		write_text(root.path_join(rel).path_join("f.txt"), rel)
	return {"root": root, "kept": kept, "old": MANAGED.path_join(key).path_join(OLD_SHA),
			"gone": MANAGED.path_join(GONE_KEY)}


func _cmd(root: String) -> RefCounted:
	return Commands.new(null, root, tmp_dir("reg"), tmp_dir("cache"))


func _exists(root: String, rel: String) -> bool:
	return DirAccess.dir_exists_absolute(root.path_join(rel))


func test_dry_run_lists_and_writes_nothing() -> void:
	var p: Dictionary = _project()
	var before: Dictionary = snapshot(p["root"])
	assert_eq(PruneCommand.run(_cmd(p["root"]), {}), 0, "default is a dry run")
	assert_eq(PruneCommand.run(_cmd(p["root"]), {"dry-run": true}), 0, "explicit dry run")
	assert_eq(snapshot(p["root"]), before, "no file changed, no .assetstudio created")
	var found: Dictionary = PruneCommand.plan(p["root"], MANAGED, Lock.parse_bytes(Fs.read_bytes(p["root"].path_join(Lock.FILE_NAME))).value)
	assert_eq(found["orphans"].size(), 2, "old manifest dir and fully orphaned asset")
	assert_true(not (found["orphans"] as Array).has(p["kept"]), "referenced dir is not a candidate")
	cleanup()


func test_apply_removes_orphans_and_keeps_referenced_and_unknown() -> void:
	var p: Dictionary = _project()
	assert_eq(PruneCommand.run(_cmd(p["root"]), {"apply": true}), 0, "apply")
	assert_true(_exists(p["root"], p["kept"]), "referenced delivery kept")
	assert_true(not _exists(p["root"], p["old"]), "superseded manifest dir removed")
	assert_true(not _exists(p["root"], p["gone"]), "orphaned asset dir removed")
	assert_true(_exists(p["root"], MANAGED.path_join("notes")), "unknown dir untouched")
	assert_true(_exists(p["root"], MANAGED.path_join(".staging/t1")), ".staging untouched")
	assert_eq(PruneCommand.run(_cmd(p["root"]), {"apply": true}), 0, "second apply is a no-op")
	assert_true(not PruneCommand.has_pending_txn(p["root"]), "no pending transaction")
	cleanup()


func test_pending_transaction_blocks_dry_run() -> void:
	var p: Dictionary = _project()
	DirAccess.make_dir_recursive_absolute(p["root"].path_join(".assetstudio/txn/t1"))
	assert_eq(PruneCommand.run(_cmd(p["root"]), {}), 1, "dry run refuses while a transaction is pending")
	cleanup()


func test_crash_mid_prune_recovers_to_old_or_new_state() -> void:
	var done: Dictionary = _project()
	var old_state: Dictionary = snapshot(done["root"], [".assetstudio"])
	assert_eq(PruneCommand.run(_cmd(done["root"]), {"apply": true}), 0, "uninterrupted apply")
	var new_state: Dictionary = snapshot(done["root"], [".assetstudio"])
	assert_true(old_state != new_state, "apply changes the tree")
	var crashed: int = 0
	for step: int in range(1, 40):
		var p: Dictionary = _project()
		Coordinator.fail_after_step = step
		var code: int = PruneCommand.run(_cmd(p["root"]), {"apply": true})
		Coordinator.fail_after_step = -1
		if code == 0:
			break
		crashed += 1
		assert_true(Coordinator.recover_project(p["root"]).ok, "recover after crash at step %d" % step)
		var state: Dictionary = snapshot(p["root"], [".assetstudio"])
		assert_true(state == old_state or state == new_state, "step %d: old or new state, never partial" % step)
	assert_true(crashed > 0, "crash points were exercised")
	cleanup()
