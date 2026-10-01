extends RefCounted
# assetstudio.project.json (addon-owned format, schema_version 1; design §2). Tracked in git, never holds secrets.
# Parsing is strict (unknown keys are errors). Hand edits are tolerated: only the writer is canonical.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")
const CJson = preload("res://addons/assetstudio/core/as_canonical_json.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")

const FILE_NAME: String = "assetstudio.project.json"
const KEYS: PackedStringArray = ["schema_version", "server_id", "libraries", "managed_root", "prefab_root",
		"material_profiles_dir", "default_material_policy"]
const POLICY_MODES: PackedStringArray = ["preserve", "project_mapping"]

var server_id: String = ""
var libraries: Array = []  # [{"library_id", "label"}]
var managed_root: String = "res://assets/library"
var prefab_root: String = "res://assets/prefabs"
var material_profiles_dir: String = "res://integration/material_profiles"
var default_material_policy: Dictionary = {"mode": "preserve", "profile_id": null}


static func defaults(for_server_id: String) -> RefCounted:
	var c: RefCounted = new()
	c.set("server_id", for_server_id)
	return c


## ASResult whose value is an ASProjectConfig.
static func parse_bytes(raw: PackedByteArray) -> RefCounted:
	var p: RefCounted = CJson.parse_strict_utf8(raw)
	if not p.ok:
		return Result.fail(Result.CODE_INVALID_PROJECT_FILE, "%s: %s" % [FILE_NAME, p.message])
	var err: String = validate(p.value)
	if err != "":
		return Result.fail(Result.CODE_INVALID_PROJECT_FILE, "%s: %s" % [FILE_NAME, err])
	var d: Dictionary = p.value
	var c: RefCounted = new()
	c.set("server_id", d["server_id"])
	c.set("libraries", d["libraries"])
	c.set("managed_root", d["managed_root"])
	c.set("prefab_root", d["prefab_root"])
	c.set("material_profiles_dir", d["material_profiles_dir"])
	c.set("default_material_policy", d["default_material_policy"])
	return Result.success(c)


static func validate(v: Variant) -> String:
	if not v is Dictionary:
		return "expected an object"
	var d: Dictionary = v
	var err: String = Schema.check_keys(d, KEYS, PackedStringArray(), "config")
	if err == "" and Schema.check_int(d["schema_version"], 1, 1, "schema_version") != "":
		err = "schema_version must be 1"
	if err == "":
		err = Schema.check_pattern(d["server_id"], "server_id", "server_id")
	if err == "":
		err = _check_libraries(d["libraries"])
	for k: String in ["managed_root", "prefab_root", "material_profiles_dir"]:
		if err == "" and (not d[k] is String or not Fs.is_safe_res_path(d[k])):
			err = "%s must be a res:// path without '..'" % k
	if err == "":
		err = _check_policy(d["default_material_policy"])
	return err


static func _check_libraries(libs: Variant) -> String:
	if not libs is Array or (libs as Array).size() > 64:
		return "libraries: expected array of at most 64"
	var seen: Dictionary = {}
	for l: Variant in libs:
		if not l is Dictionary:
			return "libraries: expected objects"
		var err: String = Schema.check_keys(l, ["library_id", "label"], PackedStringArray(), "library")
		if err == "":
			err = Schema.check_pattern(l["library_id"], "library_id", "library_id")
		if err == "" and (not l["label"] is String or (l["label"] as String).length() > 128):
			err = "library.label: expected string of at most 128 characters"
		if err == "" and seen.has(l["library_id"]):
			err = "duplicate library_id %s" % l["library_id"]
		if err != "":
			return err
		seen[l["library_id"]] = true
	return ""


static func _check_policy(p: Variant) -> String:
	if not p is Dictionary:
		return "default_material_policy: expected object"
	var err: String = Schema.check_keys(p, ["mode", "profile_id"], PackedStringArray(), "default_material_policy")
	if err != "":
		return err
	if not p["mode"] is String or not POLICY_MODES.has(p["mode"]):
		return "default_material_policy.mode: must be preserve or project_mapping"
	if p["mode"] == "preserve":
		return "" if p["profile_id"] == null else "preserve takes no profile_id"
	return Schema.check_pattern(p["profile_id"], "slug", "default_material_policy.profile_id")


func to_dict() -> Dictionary:
	return {"schema_version": 1, "server_id": server_id, "libraries": libraries, "managed_root": managed_root,
			"prefab_root": prefab_root, "material_profiles_dir": material_profiles_dir,
			"default_material_policy": default_material_policy}


## Canonical bytes (ASResult value).
func to_bytes() -> RefCounted:
	return CJson.encode(to_dict())


func has_library(library_id: String) -> bool:
	for l: Dictionary in libraries:
		if l["library_id"] == library_id:
			return true
	return false


func add_library(library_id: String, label: String) -> void:
	if not has_library(library_id):
		libraries.append({"library_id": library_id, "label": label})


## Project-relative managed root ("assets/library").
func managed_rel() -> String:
	return managed_root.trim_prefix("res://")


func prefab_rel() -> String:
	return prefab_root.trim_prefix("res://")


## Loads <root>/assetstudio.project.json. A missing file is an error (callers decide whether to create one).
static func load_from(project_root: String) -> RefCounted:
	var path: String = project_root.path_join(FILE_NAME)
	if not FileAccess.file_exists(path):
		return Result.fail(Result.CODE_INVALID_PROJECT_FILE, "%s not found; run `connect` first" % FILE_NAME)
	return parse_bytes(Fs.read_bytes(path))
