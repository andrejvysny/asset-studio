extends RefCounted
# Exact asset reference (asset-ref.schema.json). Never a "latest" pointer.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")
const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")

const FIELDS: PackedStringArray = ["server_id", "library_id", "asset_id", "version_id"]

var server_id: String = ""
var library_id: String = ""
var asset_id: String = ""
var version_id: String = ""


## Returns an ASResult whose value is an ASAssetRef.
static func parse(d: Variant) -> RefCounted:
	var err: String = validate(d)
	if err != "":
		return Result.fail(Result.CODE_INVALID_RESPONSE, err)
	var ref: RefCounted = new()
	for f: String in FIELDS:
		ref.set(f, (d as Dictionary)[f])
	return Result.success(ref)


static func validate(d: Variant, field: String = "asset_ref") -> String:
	if not d is Dictionary:
		return "%s: expected object" % field
	var dict: Dictionary = d
	var err: String = Schema.check_keys(dict, FIELDS, PackedStringArray(), field)
	if err != "":
		return err
	for f: String in FIELDS:
		err = Schema.check_pattern(dict[f], f, "%s.%s" % [field, f])
		if err != "":
			return err
	return ""


func to_dict() -> Dictionary:
	return {"server_id": server_id, "library_id": library_id, "asset_id": asset_id, "version_id": version_id}


func key() -> String:
	return Canonical.asset_key(server_id, library_id, asset_id, version_id)


func equals(other: RefCounted) -> bool:
	return other != null and to_dict() == other.call("to_dict")
