extends "res://tests/project_test_base.gd"
# Mutation coordinator: crash injection at every step, idempotent recovery, mutex contention, stale locks.

const Coordinator = preload("res://addons/assetstudio/project/as_mutation_coordinator.gd")

const SKIP_JOURNAL: PackedStringArray = [".assetstudio"]


func _seed(root: String) -> void:
	write_text(root.path_join("a.txt"), "old a")
	write_text(root.path_join("dir/c.txt"), "old c")
	write_text(root.path_join("existing/e.txt"), "old e")
	write_text(root.path_join("untouched.txt"), "same")


## Queues a mixed transaction: overwrite, create, delete, new directory, directory replacing an existing one.
func _queue(c: RefCounted) -> void:
	c.add_write("a.txt", "new a".to_utf8_buffer())
	c.add_write("sub/b.txt", "new b".to_utf8_buffer())
	c.add_delete("dir/c.txt")
	var staging: String = c.new_staging_dir("managed")
	write_text(c.abs_path(staging.path_join("one/f.txt")), "dir one")
	write_text(c.abs_path(staging.path_join("two/g.txt")), "dir two")
	c.add_dir(staging.path_join("one"), "managed/one", "")
	c.add_dir(staging.path_join("two"), "existing")


func _run_once(root: String, crash_at: int) -> Dictionary:
	_seed(root)
	var c: RefCounted = Coordinator.new(root)
	var o: RefCounted = c.open("test")
	assert_true(o.ok, "open: %s" % o.describe())
	_queue(c)
	Coordinator.fail_after_step = crash_at
	var r: RefCounted = c.commit()
	Coordinator.fail_after_step = -1
	return {"result": r, "txn": c.txn_id, "steps": c.steps_taken()}


func _sha_of(text: String) -> String:
	return Fs.sha256_bytes(text.to_utf8_buffer())


func _history_count(root: String, txn: String) -> int:
	var parsed: RefCounted = CJson.parse_canonical(Fs.read_bytes(root.path_join(".assetstudio/history.json")))
	if not parsed.ok:
		return 0
	var n: int = 0
	for e: Dictionary in parsed.value["entries"]:
		if e["id"] == txn:
			n += 1
	return n


func test_crash_at_every_step_then_recover_gives_old_or_new_state() -> void:
	var ref_root: String = tmp_dir("ref")
	var ref_run: Dictionary = _run_once(ref_root, -1)
	assert_true(ref_run["result"].ok, "uninterrupted commit: %s" % ref_run["result"].describe())
	var old_root: String = tmp_dir("old")
	_seed(old_root)
	var old_state: Dictionary = snapshot(old_root, SKIP_JOURNAL)
	var new_state: Dictionary = snapshot(ref_root, SKIP_JOURNAL)
	assert_true(old_state != new_state, "transaction changes something")
	assert_eq(read_text(ref_root.path_join("a.txt")), "new a", "a.txt rewritten")
	assert_eq(read_text(ref_root.path_join("existing/g.txt")), "dir two", "existing directory replaced")
	var total: int = ref_run["steps"]
	assert_true(total >= 10, "expected many crash points, got %d" % total)
	var saw_old: bool = false
	var saw_new: bool = false
	for n: int in range(1, total + 1):
		var root: String = tmp_dir("crash")
		var run: Dictionary = _run_once(root, n)
		assert_eq(run["result"].code, "simulated_crash", "step %d crashes" % n)
		var rec: RefCounted = Coordinator.recover_project(root)
		assert_true(rec.ok, "recover after step %d: %s" % [n, rec.describe()])
		if not rec.ok:
			continue
		var after: Dictionary = snapshot(root, SKIP_JOURNAL)
		var is_old: bool = after == old_state
		var is_new: bool = after == new_state
		assert_true(is_old != is_new, "step %d: neither exactly old nor exactly new state" % n)
		saw_old = saw_old or is_old
		saw_new = saw_new or is_new
		if is_new:
			assert_eq(rec.value[0]["action"], "completed", "step %d commit completed" % n)
			assert_eq(_history_count(root, run["txn"]), 1, "step %d: history recorded once" % n)
		else:
			assert_true(["rolled_back", "discarded"].has(rec.value[0]["action"]), "step %d rolled back" % n)
			assert_eq(_history_count(root, run["txn"]), 0, "step %d: rolled back txn leaves no history" % n)
		var again: RefCounted = Coordinator.recover_project(root)
		assert_true(again.ok and (again.value as Array).is_empty(), "step %d: second recovery is a no-op" % n)
		assert_eq(snapshot(root, SKIP_JOURNAL), after, "step %d: idempotent" % n)
		assert_true(Fs.list_dir(root.path_join(".assetstudio/txn"))["dirs"].is_empty(), "journal cleaned")
		assert_true(not DirAccess.dir_exists_absolute(root.path_join(".assetstudio/lock")), "mutex released")
	assert_true(saw_old and saw_new, "crash points on both sides of the commit marker")
	cleanup()


func test_commit_records_history_with_before_and_after_hashes() -> void:
	var root: String = tmp_dir("hist")
	var run: Dictionary = _run_once(root, -1)
	assert_true(run["result"].ok, "commit")
	var hist: Dictionary = CJson.parse_canonical(Fs.read_bytes(root.path_join(".assetstudio/history.json"))).value
	var entry: Dictionary = hist["entries"][0]
	assert_eq(entry["id"], run["txn"], "txn id")
	var by_path: Dictionary = {}
	for op: Dictionary in entry["ops"]:
		by_path[op["path"]] = op
	assert_eq(by_path["a.txt"]["before_sha256"], _sha_of("old a"), "before hash")
	assert_eq(by_path["a.txt"]["after_sha256"], _sha_of("new a"), "after hash")
	assert_eq(by_path["sub/b.txt"]["before_sha256"], null, "created file has no before hash")
	cleanup()


func test_mutex_contention_and_release() -> void:
	var root: String = tmp_dir("mutex")
	var first: RefCounted = Coordinator.new(root)
	assert_true(first.open("one").ok, "first open")
	var second: RefCounted = Coordinator.new(root)
	var r: RefCounted = second.open("two")
	assert_true(not r.ok and r.code == "mutation_locked", "second open is refused while the first holds the mutex")
	assert_true(r.retryable, "contention is retryable")
	first.close()
	assert_true(second.open("two").ok, "mutex free after close")
	second.close()
	assert_true(not DirAccess.dir_exists_absolute(root.path_join(".assetstudio/lock")), "no lock left")
	cleanup()


func test_stale_mutex_of_dead_process_is_broken() -> void:
	var root: String = tmp_dir("stale")
	var lock: String = root.path_join(".assetstudio/lock")
	DirAccess.make_dir_recursive_absolute(lock)
	write_text(lock.path_join("owner.json"), JSON.stringify({"pid": 2147483646, "operation": "ghost", "started_unix": 1}))
	var c: RefCounted = Coordinator.new(root)
	var r: RefCounted = c.open("after-crash")
	assert_true(r.ok, "stale lock broken: %s" % r.describe())
	assert_true(" ".join(c.notes).contains("stale"), "break is logged: %s" % str(c.notes))
	c.close()
	cleanup()


func test_fresh_ownerless_mutex_is_respected() -> void:
	var root: String = tmp_dir("ownerless")
	DirAccess.make_dir_recursive_absolute(root.path_join(".assetstudio/lock"))
	var r: RefCounted = Coordinator.new(root).open("x")
	assert_true(not r.ok and r.code == "mutation_locked", "a lock whose owner file is not written yet is not stolen")
	cleanup()


func test_live_pid_is_not_broken() -> void:
	var root: String = tmp_dir("live")
	var lock: String = root.path_join(".assetstudio/lock")
	DirAccess.make_dir_recursive_absolute(lock)
	write_text(lock.path_join("owner.json"), JSON.stringify({"pid": OS.get_process_id(), "operation": "me"}))
	assert_true(not Coordinator.new(root).open("x").ok, "own live pid keeps the mutex")
	cleanup()


func test_unsafe_and_duplicate_paths_are_refused_without_changes() -> void:
	var root: String = tmp_dir("unsafe")
	_seed(root)
	var before: Dictionary = snapshot(root, SKIP_JOURNAL)
	for bad: String in ["../escape.txt", "/abs.txt", "a/../b.txt", "", "a//b", "c:\\x"]:
		var c: RefCounted = Coordinator.new(root)
		assert_true(c.open("x").ok, "open")
		c.add_write(bad, PackedByteArray([1]))
		assert_true(not c.commit().ok, "refuse %s" % bad)
	var d: RefCounted = Coordinator.new(root)
	d.open("x")
	d.add_write("a.txt", PackedByteArray([1]))
	d.add_write("a.txt", PackedByteArray([2]))
	assert_true(not d.commit().ok, "duplicate targets refused")
	assert_eq(snapshot(root, SKIP_JOURNAL), before, "nothing changed")
	cleanup()


func test_abandoned_transaction_removes_staging() -> void:
	var root: String = tmp_dir("abandon")
	var c: RefCounted = Coordinator.new(root)
	c.open("x")
	var staging: String = c.new_staging_dir("managed")
	write_text(c.abs_path(staging.path_join("one/f.txt")), "x")
	c.close()
	assert_true(not DirAccess.dir_exists_absolute(c.abs_path(staging)), "staging deleted")
	assert_true(not DirAccess.dir_exists_absolute(root.path_join(".assetstudio/lock")), "mutex released")
	cleanup()


func test_recovery_runs_at_open_and_reports_what_it_did() -> void:
	var root: String = tmp_dir("open")
	var run: Dictionary = _run_once(root, 4)  # crashed mid-backup
	assert_eq(run["result"].code, "simulated_crash", "crashed")
	var c: RefCounted = Coordinator.new(root)
	var r: RefCounted = c.open("next")
	assert_true(r.ok and (r.value as Array).size() == 1, "open recovered the pending transaction")
	assert_true(" ".join(c.notes).contains("recovered"), "recovery noted")
	c.close()
	cleanup()
