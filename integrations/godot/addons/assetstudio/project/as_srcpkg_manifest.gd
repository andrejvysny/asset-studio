@tool
extends RefCounted
# Client-side check of source_manifest.json (static-source-package.schema.json, spec §2). Same codes as the server
# validator: schema problems are unsafe_package/manifest_invalid, an asset_dependency naming a key that is not in
# asset_dependencies is unsupported_source_dependency/unknown_asset_key. The deep structure of `placement` and
# `conversion_report` is checked by the server only (the client needs neither to install).

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")
const AssetRef = preload("res://addons/assetstudio/core/as_asset_ref.gd")
const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")
const CJson = preload("res://addons/assetstudio/core/as_canonical_json.gd")
const Policy = preload("res://addons/assetstudio/project/as_srcpkg_policy.gd")

const TOP_LEVEL: PackedStringArray = ["schema_version", "entry_scene", "source_godot_version", "files", "resource_map",
		"asset_dependencies", "capabilities", "conversion_report", "placement"]
const PLACEMENT_KEYS: PackedStringArray = ["placement_anchor", "footprint_radius_m", "scale_range",
		"height_offset_range_m", "default_grounding", "material_slots"]
const PORTABLE_STATUS: PackedStringArray = ["exact", "approximated", "desktop_only"]


static func _bad(message: String) -> RefCounted:
	return Result.fail("unsafe_package", message, false, {"detail": "manifest_invalid", "path": Policy.MANIFEST_NAME})


## value = the parsed manifest Dictionary. `raw` must be canonical JSON.
static func parse(raw: PackedByteArray) -> RefCounted:
	var parsed: RefCounted = CJson.parse_canonical(raw)
	if not parsed.ok:
		return _bad("source_manifest.json is not canonical JSON: %s" % parsed.message)
	if not parsed.value is Dictionary:
		return _bad("source_manifest.json is not an object")
	var d: Dictionary = parsed.value
	var err: String = Schema.check_keys(d, TOP_LEVEL, PackedStringArray(), "manifest")
	if err == "":
		err = _scalars(d)
	if err != "":
		return _bad(err)
	var files: RefCounted = _files(d)
	if not files.ok:
		return files
	var deps: String = _dependencies(d["asset_dependencies"])
	if deps != "":
		return _bad(deps)
	var rmap: RefCounted = _resource_map(d["resource_map"], files.value, d["asset_dependencies"])
	if not rmap.ok:
		return rmap
	err = _extras(d)
	return _bad(err) if err != "" else Result.success(d)


static func _scalars(d: Dictionary) -> String:
	if Schema.check_int(d["schema_version"], 1, 1, "schema_version") != "":
		return "schema_version must be 1"
	var ep: Variant = d["entry_scene"]
	var err: String = Schema.check_safe_path(ep, "entry_scene")
	if err == "" and not (ep as String).ends_with(".tscn"):
		err = "entry_scene must be a .tscn"
	if err != "":
		return err
	var v: Variant = d["source_godot_version"]
	if not v is String or not _version_ok(v):
		return "source_godot_version: invalid"
	return ""


static func _version_ok(v: String) -> bool:
	var re: RegEx = RegEx.create_from_string("^[0-9]+\\.[0-9]+(\\.[0-9]+)?(-[0-9A-Za-z.]+)?$")
	var m: RegExMatch = re.search(v)
	return m != null and m.get_string() == v


## value = {path: entry} of the declared files (also validates uniqueness, case-fold included).
static func _files(d: Dictionary) -> RefCounted:
	var files: Variant = d["files"]
	if not files is Array or (files as Array).is_empty() or (files as Array).size() > Policy.MAX_FILES:
		return _bad("files: expected 1..%d items" % Policy.MAX_FILES)
	var by_path: Dictionary = {}
	var folded: Dictionary = {}
	for f: Variant in files:
		var err: String = _file_entry(f)
		if err != "":
			return _bad(err)
		var path: String = f["path"]
		if path == Policy.MANIFEST_NAME:
			return _bad("source_manifest.json must not list itself")
		if folded.has(path.to_lower()):
			return _bad("duplicate path (case-insensitive): %s" % path)
		folded[path.to_lower()] = true
		by_path[path] = f
	if not by_path.has(d["entry_scene"]):
		return _bad("entry_scene is not listed in files")
	return Result.success(by_path)


static func _file_entry(f: Variant) -> String:
	if not f is Dictionary:
		return "file: expected object"
	var err: String = Schema.check_keys(f, ["path", "sha256", "size", "media_type"], PackedStringArray(), "file")
	if err == "":
		err = Schema.check_safe_path(f["path"], "file.path")
	if err == "":
		err = Schema.check_pattern(f["sha256"], "sha256", "file.sha256")
	if err == "":
		err = Schema.check_int(f["size"], 0, Policy.MAX_EXPANDED_BYTES, "file.size")
	if err == "":
		err = Schema.check_pattern(f["media_type"], "media_type", "file.media_type")
	return err


static func _dependencies(deps: Variant) -> String:
	if not deps is Dictionary:
		return "asset_dependencies: expected object"
	for key: Variant in (deps as Dictionary):
		var entry: Variant = deps[key]
		if not key is String or not Schema.matches("sha256", key) or not entry is Dictionary:
			return "asset_dependencies: invalid entry"
		var err: String = Schema.check_keys(entry, ["asset_ref", "descriptor_sha256", "representation", "delivery_id"],
				PackedStringArray(), "asset_dependencies entry")
		if err == "":
			err = AssetRef.validate(entry["asset_ref"], "asset_dependencies.asset_ref")
		if err == "":
			err = Schema.check_pattern(entry["descriptor_sha256"], "sha256", "asset_dependencies.descriptor_sha256")
		if err == "" and (not entry["representation"] is String or not Schema.REPRESENTATIONS.has(entry["representation"])):
			err = "asset_dependencies.representation: unknown value"
		if err == "" and entry["delivery_id"] != null:
			err = Schema.check_pattern(entry["delivery_id"], "delivery_id", "asset_dependencies.delivery_id")
		if err == "":
			var r: Dictionary = entry["asset_ref"]
			if Canonical.asset_key(r["server_id"], r["library_id"], r["asset_id"], r["version_id"]) != key:
				err = "asset_dependencies key does not match its asset_ref"
		if err != "":
			return err
	return ""


static func _resource_map(rmap: Variant, files: Dictionary, deps: Dictionary) -> RefCounted:
	if not rmap is Dictionary:
		return _bad("resource_map: expected object")
	for ref: Variant in (rmap as Dictionary):
		if not ref is String or not _ref_key_ok(ref) or not rmap[ref] is Dictionary:
			return _bad("resource_map: invalid reference key")
		var e: Dictionary = rmap[ref]
		var err: String = _check_entry(e)
		if err != "":
			return _bad("resource_map[%s]: %s" % [ref, err])
		if e["kind"] == "package_file" and not files.has(e["path"]):
			return _bad("resource_map[%s]: path is not listed in files" % ref)
		if e["kind"] == "asset_dependency" and not deps.has(e["asset_key"]):
			return Result.fail("unsupported_source_dependency", "resource_map[%s] names asset_key %s which is not in asset_dependencies" % [ref, e["asset_key"]],
					false, {"detail": "unknown_asset_key", "path": Policy.MANIFEST_NAME})
	return Result.success()


## res:// prefix, no ".." segment, no backslash or control characters.
static func _ref_key_ok(ref: String) -> bool:
	if not ref.begins_with("res://") or ref.length() <= 6 or ref.contains("\\"):
		return false
	for i: int in ref.length():
		if ref.unicode_at(i) < 0x20:
			return false
	return not ref.substr(6).split("/").has("..")


static func _check_entry(e: Dictionary) -> String:
	var kind: Variant = e.get("kind")
	var err: String = ""
	if kind == "package_file":
		err = Schema.check_keys(e, ["kind", "path", "original_uid"], PackedStringArray(), "entry")
		if err == "":
			err = Schema.check_safe_path(e["path"], "path")
	elif kind == "asset_dependency":
		err = Schema.check_keys(e, ["kind", "asset_key", "entrypoint", "original_uid"], PackedStringArray(), "entry")
		if err == "":
			err = Schema.check_pattern(e["asset_key"], "sha256", "asset_key")
		if err == "":
			err = Schema.check_safe_path(e["entrypoint"], "entrypoint")
	else:
		return "unknown kind"
	if err == "" and e["original_uid"] != null and not _uid_ok(e["original_uid"]):
		err = "original_uid: invalid"
	return err


static func _uid_ok(v: Variant) -> bool:
	if not v is String or not (v as String).begins_with("uid://"):
		return false
	var body: String = (v as String).substr(6)
	if body.is_empty() or body.length() > 32:
		return false
	for i: int in body.length():
		var c: int = body.unicode_at(i)
		if not ((c >= 97 and c <= 122) or (c >= 48 and c <= 57)):
			return false
	return true


static func _extras(d: Dictionary) -> String:
	var caps: Variant = d["capabilities"]
	if not caps is Array:
		return "capabilities: expected array"
	var seen: Dictionary = {}
	for c: Variant in caps:
		if not c is String or not Policy.KNOWN_CAPABILITIES.has(c) or seen.has(c):
			return "capabilities: unknown or duplicate capability"
		seen[c] = true
	var rep: Variant = d["conversion_report"]
	if not rep is Dictionary or not (rep as Dictionary).get("portable_status") in PORTABLE_STATUS:
		return "conversion_report: invalid"
	var err: String = Schema.check_keys(rep, ["portable_status", "omissions", "approximations"], PackedStringArray(), "conversion_report")
	if err != "":
		return err
	var pl: Variant = d["placement"]
	if not pl is Dictionary:
		return "placement: expected object"
	return Schema.check_keys(pl, PLACEMENT_KEYS, PackedStringArray(), "placement")
