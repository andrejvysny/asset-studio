@tool
extends RefCounted
# Strict DeliveryManifestV1 parser (delivery-manifest.schema.json). Parses RAW bytes and hashes the raw
# bytes. Rejects unknown fields (so any URL-ish field), unsafe/duplicate paths and non-matching dependency keys.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")
const AssetRef = preload("res://addons/assetstudio/core/as_asset_ref.gd")
const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")

const REQUIRED: PackedStringArray = [
	"schema_version", "delivery_id", "asset_ref", "descriptor_sha256", "representation", "profile_id",
	"profile_version", "preparer", "entrypoint", "files", "dependencies", "required_capabilities",
]
const FILE_KEYS: PackedStringArray = ["path", "sha256", "size", "media_type", "artifact_id"]
const DEP_KEYS: PackedStringArray = [
	"asset_key", "asset_ref", "descriptor_sha256", "representation", "delivery_id", "manifest_sha256",
]
const MAX_FILES: int = 4096
const MAX_FILE_SIZE: int = 1073741824

var raw_sha256: String = ""
var asset_ref: RefCounted = null
var data: Dictionary = {}


## Returns an ASResult whose value is an ASDeliveryManifest (raw_sha256 = sha256 of `raw`).
static func parse_bytes(raw: PackedByteArray) -> RefCounted:
	var parsed: Dictionary = Schema.parse_json_bytes(raw)
	if not parsed["ok"]:
		return Result.fail(Result.CODE_INVALID_RESPONSE, "manifest: %s" % parsed["error"])
	var d: Dictionary = parsed["value"]
	var err: String = validate(d)
	if err != "":
		return Result.fail(Result.CODE_INVALID_RESPONSE, "manifest: %s" % err)
	var m: RefCounted = new()
	m.set("raw_sha256", Canonical.sha256_hex(raw))
	m.set("data", d)
	m.set("asset_ref", AssetRef.parse(d["asset_ref"]).value)
	return Result.success(m)


static func validate(d: Dictionary) -> String:
	var err: String = Schema.check_keys(d, REQUIRED, PackedStringArray(), "manifest")
	if err == "" and Schema.check_int(d["schema_version"], 1, 1, "schema_version") != "":
		err = "schema_version must be 1"
	if err == "":
		err = AssetRef.validate(d["asset_ref"])
	if err == "":
		err = _check_scalars(d)
	if err == "":
		err = _check_files(d)
	if err == "":
		err = _check_dependencies(d["dependencies"])
	if err == "":
		err = _check_capabilities(d["required_capabilities"])
	return err


static func _check_scalars(d: Dictionary) -> String:
	var checks: Array = [["delivery_id", "delivery_id"], ["descriptor_sha256", "sha256"], ["profile_id", "slug"],
			["profile_version", "slug"]]
	for c: Array in checks:
		var err: String = Schema.check_pattern(d[c[0]], c[1], c[0])
		if err != "":
			return err
	if not d["representation"] is String or not Schema.REPRESENTATIONS.has(d["representation"]):
		return "representation: unknown value"
	var prep: Variant = d["preparer"]
	if not prep is Dictionary:
		return "preparer: expected object"
	var err2: String = Schema.check_keys(prep, ["name", "version"], PackedStringArray(), "preparer")
	if err2 == "":
		err2 = Schema.check_pattern(prep["name"], "slug", "preparer.name")
	if err2 == "":
		err2 = Schema.check_pattern(prep["version"], "version_text", "preparer.version")
	return err2


static func _check_files(d: Dictionary) -> String:
	var err: String = Schema.check_safe_path(d["entrypoint"], "entrypoint")
	if err != "":
		return err
	var files: Variant = d["files"]
	if not files is Array or (files as Array).is_empty() or (files as Array).size() > MAX_FILES:
		return "files: expected 1..%d items" % MAX_FILES
	var folded: Dictionary = {}
	for f: Variant in files:
		err = _check_file(f)
		if err != "":
			return err
		var key: String = (f["path"] as String).to_lower()
		if folded.has(key):
			return "duplicate path (case-insensitive): %s" % f["path"]
		folded[key] = f["path"]
	for f: Dictionary in files:
		if f["path"] == d["entrypoint"]:
			return ""
	return "entrypoint is not listed in files"


static func _check_file(f: Variant) -> String:
	if not f is Dictionary:
		return "file: expected object"
	var file: Dictionary = f
	var err: String = Schema.check_keys(file, FILE_KEYS, PackedStringArray(), "file")
	if err == "":
		err = Schema.check_safe_path(file["path"], "file.path")
	if err == "":
		err = Schema.check_pattern(file["sha256"], "sha256", "file.sha256")
	if err == "":
		err = Schema.check_int(file["size"], 0, MAX_FILE_SIZE, "file.size")
	if err == "" and (not file["media_type"] is String or (file["media_type"] as String).length() > 127):
		err = "file.media_type: too long"
	if err == "":
		err = Schema.check_pattern(file["media_type"], "media_type", "file.media_type")
	if err == "":
		err = Schema.check_pattern(file["artifact_id"], "artifact_id", "file.artifact_id")
	return err


static func _check_dependencies(deps: Variant) -> String:
	if not deps is Array or (deps as Array).size() > MAX_FILES:
		return "dependencies: expected array"
	for dep: Variant in deps:
		if not dep is Dictionary:
			return "dependency: expected object"
		var dd: Dictionary = dep
		var err: String = Schema.check_keys(dd, DEP_KEYS, PackedStringArray(), "dependency")
		if err == "":
			err = AssetRef.validate(dd["asset_ref"], "dependency.asset_ref")
		for k: String in ["asset_key", "descriptor_sha256", "manifest_sha256"]:
			if err == "":
				err = Schema.check_pattern(dd[k], "sha256", "dependency.%s" % k)
		if err == "":
			err = Schema.check_pattern(dd["delivery_id"], "delivery_id", "dependency.delivery_id")
		if err == "" and (not dd["representation"] is String or not Schema.REPRESENTATIONS.has(dd["representation"])):
			err = "dependency.representation: unknown value"
		if err != "":
			return err
		var r: Dictionary = dd["asset_ref"]
		if Canonical.asset_key(r["server_id"], r["library_id"], r["asset_id"], r["version_id"]) != dd["asset_key"]:
			return "dependency.asset_key does not match asset_ref"
	return ""


static func _check_capabilities(caps: Variant) -> String:
	if not caps is Array:
		return "required_capabilities: expected array"
	var seen: Dictionary = {}
	for c: Variant in caps:
		if not c is String or not Schema.CAPABILITIES.has(c) or seen.has(c):
			return "required_capabilities: unknown or duplicate capability"
		seen[c] = true
	return ""


func file_entries() -> Array:
	return data["files"]
