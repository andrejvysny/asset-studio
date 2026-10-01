extends RefCounted
# Single writer for every file the addon changes in a consumer project (docs/integration/as-07-08-design.md §6).
#
# Usage (one instance per transaction):
#   var c = Coordinator.new(project_root_abs)
#   var r = c.open("add")                       # takes the mutex, then recovers any crashed transaction
#   c.add_write("assetstudio.lock.json", bytes)
#   c.add_dir(staged_rel, target_rel, receipt_sha)   # rename a pre-built directory into place
#   c.add_delete("old/file.tres")
#   r = c.commit()                              # intent -> stage -> backup -> apply -> COMMITTED -> cleanup
#   c.close()                                   # no-op after commit; releases the mutex if commit never ran
#
# Layout (all under <root>/.assetstudio/, git-ignored): lock/ (mutex dir + owner.json), txn/<id>/{intent.json,
# stage/, backup/, COMMITTED}, history.json. Every step is atomic (rename or tmp+rename), and all backups exist
# before the first target is touched, so after recover() the project is exactly in the old or the new state.
# Paths in ops are project-relative with "/" separators.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")
const CJson = preload("res://addons/assetstudio/core/as_canonical_json.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")

const META_DIR: String = ".assetstudio"
const MAX_HISTORY: int = 200
const STALE_OWNERLESS_SECONDS: int = 120

## Test hook: simulate a crash once this many atomic steps have run (-1 = off). The simulated crash leaves the
## journal exactly as a dead process would, and releases the mutex that a stale-lock break would clear anyway.
static var fail_after_step: int = -1

var root: String = ""
var txn_id: String = ""
var operation: String = ""
## Human-readable events (stale mutex broken, transaction recovered). Callers may print them.
var notes: PackedStringArray = PackedStringArray()

var _held: bool = false
var _step: int = 0
var _crashed: bool = false
var _ops: Array[Dictionary] = []
var _staging_dirs: PackedStringArray = PackedStringArray()


func _init(project_root: String) -> void:
	root = project_root.simplify_path().trim_suffix("/")


# --- public API ----------------------------------------------------------------------------------------------

## Takes the mutex and recovers pending transactions. value = Array of recovery summaries.
func open(op: String) -> RefCounted:
	operation = op
	var r: RefCounted = _acquire()
	if not r.ok:
		return r
	_held = true
	var rec: RefCounted = _recover_all()
	if not rec.ok:
		_release()
		return rec
	txn_id = "t%d-%d-%06x" % [int(Time.get_unix_time_from_system()), OS.get_process_id(), randi() & 0xFFFFFF]
	return rec


## Abandons an uncommitted transaction: staged directories are deleted, the mutex is released.
func close() -> void:
	if _held:
		for rel: String in _staging_dirs:
			_remove_staging(rel)
	_release()


## Recovery without making changes of its own (plugin enable, start of every CLI command).
static func recover_project(project_root: String) -> RefCounted:
	var c: RefCounted = new(project_root)
	var r: RefCounted = c.call("open", "recover")
	c.call("close")
	return r


func add_write(rel: String, data: PackedByteArray) -> void:
	_ops.append({"kind": "write", "path": rel, "data": data, "staged": "", "after_sha256": Fs.sha256_bytes(data)})


func add_delete(rel: String) -> void:
	_ops.append({"kind": "delete", "path": rel, "staged": "", "after_sha256": null})


## Renames the pre-built directory `staged_rel` to `target_rel` at apply time (never overwrites silently: an
## existing target is backed up and restored on rollback).
func add_dir(staged_rel: String, target_rel: String, after_sha256: String = "") -> void:
	_ops.append({"kind": "rename_dir", "path": target_rel, "staged": staged_rel,
			"after_sha256": after_sha256 if after_sha256 != "" else null})


## Registers and returns "<managed_rel>/.staging/<txn id>"; removed on commit and on rollback.
func new_staging_dir(managed_rel: String) -> String:
	var rel: String = managed_rel.path_join(".staging").path_join(txn_id)
	if not _staging_dirs.has(rel):
		_staging_dirs.append(rel)
	return rel


func abs_path(rel: String) -> String:
	return root.path_join(rel)


func commit() -> RefCounted:
	if not _held:
		return Result.fail("invalid_request", "transaction is not open")
	var r: RefCounted = _run_commit()
	_release()
	return r


# --- commit phases -------------------------------------------------------------------------------------------

func _run_commit() -> RefCounted:
	var built: RefCounted = _build_intent()
	if not built.ok:
		return built
	var intent: Dictionary = built.value
	var tdir: String = _txn_abs(txn_id)
	var steps: Array[Callable] = [_phase_intent, _phase_stage, _phase_backup, _phase_apply]
	for phase: Callable in steps:
		var r: RefCounted = phase.call(intent, tdir)
		if not r.ok:
			if not _crashed:
				_rollback(intent, tdir)
				Fs.remove_tree(tdir)
			return r
	return _phase_commit_and_cleanup(intent, tdir)


func _phase_intent(intent: Dictionary, tdir: String) -> RefCounted:
	var enc: RefCounted = CJson.encode(intent)
	if not enc.ok:
		return enc
	if Fs.write_atomic(tdir.path_join("intent.json"), enc.value) != OK:
		return Result.fail(Result.CODE_IO_ERROR, "cannot write transaction intent")
	return _tick_result()


func _phase_stage(intent: Dictionary, tdir: String) -> RefCounted:
	for i: int in _ops.size():
		if _ops[i]["kind"] != "write":
			continue
		if Fs.write_atomic(tdir.path_join("stage").path_join(str(i)), _ops[i]["data"]) != OK:
			return Result.fail(Result.CODE_IO_ERROR, "cannot stage %s" % _ops[i]["path"])
		var t: RefCounted = _tick_result()
		if not t.ok:
			return t
	return Result.success()


func _phase_backup(intent: Dictionary, tdir: String) -> RefCounted:
	var ops: Array = intent["ops"]
	for i: int in ops.size():
		var target: String = abs_path(ops[i]["path"])
		if not Fs.exists(target):
			continue
		var backup: String = tdir.path_join("backup").path_join(str(i))
		var err: int = OK
		if DirAccess.dir_exists_absolute(target):
			DirAccess.make_dir_recursive_absolute(backup.get_base_dir())
			err = DirAccess.rename_absolute(target, backup)
		else:
			err = Fs.copy_file_atomic(target, backup)
		if err != OK:
			return Result.fail(Result.CODE_IO_ERROR, "cannot back up %s" % ops[i]["path"])
		var t: RefCounted = _tick_result()
		if not t.ok:
			return t
	return Result.success()


func _phase_apply(intent: Dictionary, tdir: String) -> RefCounted:
	var ops: Array = intent["ops"]
	for i: int in ops.size():
		var target: String = abs_path(ops[i]["path"])
		var err: int = _apply_op(ops[i], i, target, tdir)
		if err != OK:
			return Result.fail(Result.CODE_IO_ERROR, "cannot apply %s" % ops[i]["path"])
		var t: RefCounted = _tick_result()
		if not t.ok:
			return t
	return Result.success()


func _apply_op(op: Dictionary, i: int, target: String, tdir: String) -> int:
	match op["kind"]:
		"write":
			DirAccess.make_dir_recursive_absolute(target.get_base_dir())
			return DirAccess.rename_absolute(tdir.path_join("stage").path_join(str(i)), target)
		"rename_dir":
			DirAccess.make_dir_recursive_absolute(target.get_base_dir())
			return DirAccess.rename_absolute(abs_path(op["staged"]), target)
		"delete":
			Fs.remove_tree(target)
			return OK
	return ERR_INVALID_PARAMETER


func _phase_commit_and_cleanup(intent: Dictionary, tdir: String) -> RefCounted:
	if Fs.write_atomic(tdir.path_join("COMMITTED"), "ok".to_utf8_buffer()) != OK:
		_rollback(intent, tdir)  # not committed yet: the marker is the commit point
		Fs.remove_tree(tdir)
		return Result.fail(Result.CODE_IO_ERROR, "cannot write commit marker")
	var t: RefCounted = _tick_result()
	if not t.ok:
		return t
	_append_history(intent)
	t = _tick_result()
	if not t.ok:
		return t
	_cleanup_committed(intent, tdir)
	return Result.success({"id": txn_id, "ops": (intent["ops"] as Array).size()})


# --- intent --------------------------------------------------------------------------------------------------

func _build_intent() -> RefCounted:
	var seen: Dictionary = {}
	var ops: Array = []
	for i: int in _ops.size():
		var op: Dictionary = _ops[i]
		var bad: String = _check_rel(op["path"])
		if bad == "" and op["kind"] == "rename_dir":
			bad = _check_rel(op["staged"])
		if bad != "":
			return Result.fail("invalid_request", bad)
		if seen.has(op["path"]):
			return Result.fail("invalid_request", "duplicate transaction target %s" % op["path"])
		seen[op["path"]] = true
		var target: String = abs_path(op["path"])
		var before: Variant = Fs.sha256_file(target) if FileAccess.file_exists(target) else null
		ops.append({"kind": op["kind"], "path": op["path"], "before_exists": Fs.exists(target),
				"before_sha256": before, "after_sha256": op["after_sha256"],
				"staged": ("stage/%d" % i) if op["kind"] == "write" else op["staged"]})
	return Result.success({"schema_version": 1, "id": txn_id, "operation": operation,
			"pid": OS.get_process_id(), "created_unix": int(Time.get_unix_time_from_system()), "ops": ops,
			"staging_dirs": Array(_staging_dirs)})


static func _check_rel(rel: String) -> String:
	if rel.is_empty() or rel.begins_with("/") or rel.contains("\\") or rel.contains(":"):
		return "unsafe transaction path: %s" % rel
	for seg: String in rel.split("/"):
		if seg.is_empty() or seg == "." or seg == "..":
			return "unsafe transaction path: %s" % rel
	return ""


# --- rollback, cleanup, history ------------------------------------------------------------------------------

func _rollback(intent: Dictionary, tdir: String) -> void:
	var ops: Array = intent["ops"]
	for k: int in ops.size():
		var i: int = ops.size() - 1 - k
		_rollback_op(ops[i], abs_path(ops[i]["path"]), tdir.path_join("backup").path_join(str(i)))
	for rel: String in intent.get("staging_dirs", []):
		_remove_staging(rel)


func _rollback_op(op: Dictionary, target: String, backup: String) -> void:
	if Fs.exists(backup):
		if DirAccess.dir_exists_absolute(backup):
			Fs.remove_tree(target)
			DirAccess.rename_absolute(backup, target)
		else:
			Fs.copy_file_atomic(backup, target)
		return
	if op["before_exists"]:
		return  # never backed up, so never touched
	if op["kind"] == "rename_dir":
		# The staged directory is gone only once the rename has been applied.
		if not Fs.exists(abs_path(op["staged"])):
			Fs.remove_tree(target)
		else:
			Fs.remove_tree(abs_path(op["staged"]))
	else:
		Fs.remove_tree(target)


func _cleanup_committed(intent: Dictionary, tdir: String) -> void:
	for rel: String in intent.get("staging_dirs", []):
		_remove_staging(rel)
	Fs.remove_tree(tdir)


## Deletes one staging directory, then its ".staging" parent if that is now empty (remove_absolute fails on
## non-empty directories, which is exactly the check we want).
func _remove_staging(rel: String) -> void:
	Fs.remove_tree(abs_path(rel))
	DirAccess.remove_absolute(abs_path(rel).get_base_dir())


func _append_history(intent: Dictionary) -> void:
	var path: String = root.path_join(META_DIR).path_join("history.json")
	var doc: Dictionary = {"schema_version": 1, "entries": []}
	var cur: RefCounted = CJson.parse_canonical(Fs.read_bytes(path))
	if cur.ok and cur.value is Dictionary and (cur.value as Dictionary).get("entries") is Array:
		doc = cur.value
	var entries: Array = doc["entries"]
	for e: Variant in entries:
		if e is Dictionary and e.get("id") == intent["id"]:
			return
	var ops: Array = []
	for op: Dictionary in intent["ops"]:
		ops.append({"kind": op["kind"], "path": op["path"], "before_sha256": op["before_sha256"],
				"after_sha256": op["after_sha256"]})
	entries.append({"id": intent["id"], "operation": intent["operation"],
			"time": Time.get_datetime_string_from_system(true), "ops": ops})
	while entries.size() > MAX_HISTORY:
		entries.pop_front()
	var enc: RefCounted = CJson.encode(doc)
	if enc.ok:
		Fs.write_atomic(path, enc.value)


# --- recovery ------------------------------------------------------------------------------------------------

func _recover_all() -> RefCounted:
	var summaries: Array = []
	var txn_root: String = root.path_join(META_DIR).path_join("txn")
	for id: String in (Fs.list_dir(txn_root)["dirs"] as PackedStringArray):
		var r: RefCounted = _recover_one(id)
		if not r.ok:
			return r
		summaries.append(r.value)
		notes.append("recovered transaction %s: %s" % [id, r.value["action"]])
	return Result.success(summaries)


func _recover_one(id: String) -> RefCounted:
	var tdir: String = _txn_abs(id)
	var parsed: RefCounted = CJson.parse_canonical(Fs.read_bytes(tdir.path_join("intent.json")))
	if not parsed.ok or not parsed.value is Dictionary or not (parsed.value as Dictionary).get("ops") is Array:
		if FileAccess.file_exists(tdir.path_join("intent.json")):
			return Result.fail(Result.CODE_JOURNAL_CORRUPT, "unreadable intent in transaction %s" % id)
		Fs.remove_tree(tdir)  # crashed before the intent was durable: nothing was touched
		return Result.success({"id": id, "action": "discarded", "verified": true})
	var intent: Dictionary = parsed.value
	if FileAccess.file_exists(tdir.path_join("COMMITTED")):
		_append_history(intent)
		_cleanup_committed(intent, tdir)
		return Result.success({"id": id, "action": "completed", "verified": true})
	_rollback(intent, tdir)
	var bad: String = _verify_before(intent)
	if bad != "":
		return Result.fail(Result.CODE_JOURNAL_CORRUPT, "after rollback of %s: %s (journal kept in %s)" % [id, bad, tdir])
	Fs.remove_tree(tdir)
	return Result.success({"id": id, "action": "rolled_back", "verified": true})


func _verify_before(intent: Dictionary) -> String:
	for op: Dictionary in intent["ops"]:
		var target: String = abs_path(op["path"])
		if op["before_exists"] != Fs.exists(target):
			return "%s existence differs from before state" % op["path"]
		if op["before_sha256"] != null and Fs.sha256_file(target) != op["before_sha256"]:
			return "%s content differs from before state" % op["path"]
	return ""


# --- mutex ---------------------------------------------------------------------------------------------------

func _lock_dir() -> String:
	return root.path_join(META_DIR).path_join("lock")


func _txn_abs(id: String) -> String:
	return root.path_join(META_DIR).path_join("txn").path_join(id)


func _acquire() -> RefCounted:
	DirAccess.make_dir_recursive_absolute(root.path_join(META_DIR))
	var lock: String = _lock_dir()
	var holder: String = "unknown"
	for attempt: int in 2:
		var err: int = DirAccess.make_dir_absolute(lock)
		if err == OK:
			return _write_owner(lock)
		if err != ERR_ALREADY_EXISTS:
			return Result.fail(Result.CODE_IO_ERROR, "cannot create mutex directory")
		var owner: Dictionary = _read_owner(lock)
		holder = "pid %s (%s)" % [str(owner.get("pid", "?")), str(owner.get("operation", "?"))]
		if not _break_if_stale(lock, owner):
			break
	return Result.fail(Result.CODE_LOCKED, "another mutation is in progress: %s" % holder, true)


func _write_owner(lock: String) -> RefCounted:
	var owner: Dictionary = {"pid": OS.get_process_id(), "operation": operation,
			"started_unix": int(Time.get_unix_time_from_system())}
	var enc: RefCounted = CJson.encode(owner)
	if Fs.write_atomic(lock.path_join("owner.json"), enc.value) != OK:
		Fs.remove_tree(lock)
		return Result.fail(Result.CODE_IO_ERROR, "cannot write mutex owner")
	return Result.success()


func _read_owner(lock: String) -> Dictionary:
	var p: RefCounted = CJson.parse_strict_utf8(Fs.read_bytes(lock.path_join("owner.json")))
	return p.value if p.ok and p.value is Dictionary else {}


## Moves a stale mutex aside atomically (only one breaker wins), then deletes it.
func _break_if_stale(lock: String, owner: Dictionary) -> bool:
	var stale: bool = false
	if owner.has("pid"):
		stale = not pid_alive(int(owner["pid"]))
	else:
		stale = Time.get_unix_time_from_system() - FileAccess.get_modified_time(lock) > STALE_OWNERLESS_SECONDS
	if not stale:
		return false
	var aside: String = "%s.stale-%06x" % [lock, randi() & 0xFFFFFF]
	if DirAccess.rename_absolute(lock, aside) != OK:
		return true  # somebody else broke it first; just retry the mkdir
	var moved: Dictionary = _read_owner(aside)
	if moved.get("pid") != owner.get("pid"):
		DirAccess.rename_absolute(aside, lock)  # we grabbed a live, freshly taken mutex: put it back
		return false
	Fs.remove_tree(aside)
	notes.append("broke stale mutex of dead pid %s (%s)" % [str(owner.get("pid", "?")), str(owner.get("operation", "?"))])
	return true


func _release() -> void:
	if _held:
		Fs.remove_tree(_lock_dir())
		_held = false


static func pid_alive(pid: int) -> bool:
	if pid == OS.get_process_id():
		return true
	var out: Array = []
	if OS.get_name() == "Windows":
		OS.execute("tasklist", ["/FI", "PID eq %d" % pid, "/NH"], out, true)
		return not out.is_empty() and str(out[0]).contains(str(pid))
	var code: int = OS.execute("kill", ["-0", str(pid)], out, true)
	return code == 0 or code == -1  # -1: cannot tell, so assume alive rather than steal a live mutex


# --- crash injection -----------------------------------------------------------------------------------------

func _tick_result() -> RefCounted:
	_step += 1
	if _step == fail_after_step:
		_crashed = true
		_release()
		return Result.fail("simulated_crash", "crash injected after step %d" % _step)
	return Result.success()


## Number of atomic steps taken so far (tests use it to learn how many crash points a commit has).
func steps_taken() -> int:
	return _step
