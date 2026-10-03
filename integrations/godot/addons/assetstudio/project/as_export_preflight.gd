@tool
extends RefCounted
# `export-preflight [--preset <name>] [--offline]` (spec 00 §7, AS-10). Pure file inspection: never touches the
# network, never writes. The report is machine-readable JSON; ok=false (exit 1) blocks release automation when any
# managed dependency is missing, modified or not imported, the project has an unfinished mutation, or the export
# presets / scenes would ship private files or addon editor/network scripts.
#
# problems = [{"code", "message"}]; codes: invalid_project_file, interrupted_mutation, pending_finalize,
# dependency_integrity, import_incomplete, wrapper_modified, export_presets_missing, preset_missing_exclude,
# preset_excludes_managed, forbidden_reference.

const Config = preload("res://addons/assetstudio/project/as_project_config.gd")
const Lock = preload("res://addons/assetstudio/project/as_project_lock.gd")
const Restore = preload("res://addons/assetstudio/project/as_restore.gd")
const Installer = preload("res://addons/assetstudio/project/as_installer.gd")
const State = preload("res://addons/assetstudio/project/as_project_state.gd")
const Presets = preload("res://addons/assetstudio/project/as_export_presets.gd")
const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const CJson = preload("res://addons/assetstudio/core/as_canonical_json.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")

const IMPORTED_EXTENSIONS: PackedStringArray = ["glb", "png", "jpg", "jpeg", "webp"]


static func _p(code: String, message: String) -> Dictionary:
	return {"code": code, "message": message}


## {"schema_version", "command", "ok", "offline", "preset", "checked": {...}, "problems": [...]}.
static func run(root: String, preset: String, offline: bool) -> Dictionary:
	var problems: Array = []
	var checked: Dictionary = {"deliveries": 0, "presets": 0, "files": 0}
	problems.append_array(_mutation_problems(root))
	var cfg: RefCounted = Config.load_from(root)
	var lock: RefCounted = _load_lock(root)
	if not cfg.ok:
		problems.append(_p("invalid_project_file", cfg.describe()))
	elif not lock.ok:
		problems.append(_p("invalid_project_file", lock.describe()))
	else:
		problems.append_array(_dependency_problems(root, cfg.value, lock.value, checked))
		problems.append_array(_state_problems(root, lock.value))
		problems.append_array(_preset_problems(root, cfg.value, preset, checked))
	var refs: Dictionary = Presets.reference_problems(root)
	checked["files"] = refs["files"]
	problems.append_array(refs["problems"])
	return {"schema_version": 1, "command": "export-preflight", "ok": problems.is_empty(), "offline": offline,
			"preset": preset, "checked": checked, "problems": problems}


static func _load_lock(root: String) -> RefCounted:
	var path: String = root.path_join(Lock.FILE_NAME)
	if not FileAccess.file_exists(path):
		return Result.fail(Result.CODE_INVALID_PROJECT_FILE, "%s not found" % Lock.FILE_NAME)
	return Lock.parse_bytes(Fs.read_bytes(path))


static func _mutation_problems(root: String) -> Array:
	var meta: String = root.path_join(".assetstudio")
	var out: Array = []
	if not (Fs.list_dir(meta.path_join("txn"))["dirs"] as PackedStringArray).is_empty():
		out.append(_p("interrupted_mutation", "an unfinished transaction is in .assetstudio/txn (run restore --locked)"))
	if DirAccess.dir_exists_absolute(meta.path_join("lock")):
		out.append(_p("interrupted_mutation", "the mutation lock is held (.assetstudio/lock)"))
	return out


## Every delivery of every binding closure and lock root closure: installed, receipt-intact (installed and rewritten
## files), imported.
static func _dependency_problems(root: String, cfg: RefCounted, lock: RefCounted, checked: Dictionary) -> Array:
	var needs: Array = _needs(lock)
	checked["deliveries"] = needs.size()
	var out: Array = []
	var r: RefCounted = Restore.verify_locked(root, lock, cfg, needs)
	if not r.ok:
		for msg: Variant in (r.details as Dictionary).get("problems", [r.message]):
			out.append(_p("dependency_integrity", str(msg)))
	for need: Dictionary in needs:
		var locked: Dictionary = (lock.call("dependencies") as Dictionary)[need["key"]]["deliveries"][need["representation"]]
		var dir: String = root.path_join(Installer.target_rel(cfg.get("managed_root").trim_prefix("res://"),
				need["key"], locked["manifest_sha256"]))
		if DirAccess.dir_exists_absolute(dir):
			for f: String in _unimported(root, dir):
				out.append(_p("import_incomplete", "%s/%s: %s is not imported yet (run a headless import)" % [need["key"].left(12), need["representation"], f]))
	return out


## Lock needs plus every locked delivery of each lock root's closure.
static func _needs(lock: RefCounted) -> Array:
	var seen: Dictionary = {}
	var out: Array = []
	for need: Dictionary in lock.call("needed_deliveries"):
		seen["%s|%s" % [need["key"], need["representation"]]] = true
		out.append(need)
	for r: Dictionary in (lock.doc["roots"] as Array):
		for root_key: String in r["asset_keys"]:
			for k: String in lock.call("closure", root_key):
				for rep: String in (lock.call("dependencies") as Dictionary)[k]["deliveries"]:
					if not seen.has("%s|%s" % [k, rep]):
						seen["%s|%s" % [k, rep]] = true
						out.append({"key": k, "representation": rep})
	return out


## Receipt files Godot imports that have no real import result: no .import, no [remap] (the installer only
## pre-seeds [params]) or a remapped file missing from .godot/imported.
static func _unimported(root: String, dir: String) -> PackedStringArray:
	var out := PackedStringArray()
	var parsed: RefCounted = CJson.parse_strict_utf8(Fs.read_bytes(dir.path_join(Installer.RECEIPT_NAME)))
	if not parsed.ok or not parsed.value is Dictionary:
		return out
	for f: Variant in (parsed.value as Dictionary).get("files", []):
		var rel: String = str(f.get("path", "")) if f is Dictionary else ""
		if IMPORTED_EXTENSIONS.has(rel.get_extension().to_lower()) and not _has_import_result(root, dir.path_join(rel) + ".import"):
			out.append(rel)
	return out


static func _has_import_result(root: String, import_file: String) -> bool:
	if not FileAccess.file_exists(import_file):
		return false
	var cfg := ConfigFile.new()
	if cfg.load(import_file) != OK or not cfg.has_section("remap") or str(cfg.get_value("remap", "valid", true)) == "false":
		return false
	for key: String in cfg.get_section_keys("remap"):
		var v: String = str(cfg.get_value("remap", key, ""))
		if key.begins_with("path") and v.begins_with("res://") and FileAccess.file_exists(Fs.res_to_abs(root, v)):
			return true
	return false


## Bindings awaiting finalize, and wrappers the addon wrote that were edited or deleted since.
static func _state_problems(root: String, lock: RefCounted) -> Array:
	var out: Array = []
	var pending: Array = State.read_state(root)["pending_import"]
	if not pending.is_empty():
		out.append(_p("pending_finalize", "bindings awaiting finalize: %s" % ", ".join(pending)))
	for bid: String in (lock.call("bindings") as Dictionary):
		var rec: Dictionary = State.wrapper_record(root, bid)
		if rec.is_empty():
			continue
		var path: String = root.path_join(str(rec.get("path", "")))
		if Fs.sha256_file(path) != str(rec.get("sha256", "")):
			out.append(_p("wrapper_modified", "wrapper of binding %s is missing or was edited (%s)" % [bid, rec.get("path", "")]))
	return out


static func _preset_problems(root: String, cfg: RefCounted, preset: String, checked: Dictionary) -> Array:
	var loaded: Dictionary = Presets.load_presets(root, preset)
	var out: Array = (loaded["problems"] as Array).duplicate()
	checked["presets"] = (loaded["presets"] as Array).size()
	for p: Dictionary in loaded["presets"]:
		out.append_array(Presets.preset_problems(p, str(cfg.get("managed_root")).trim_prefix("res://")))
	return out
