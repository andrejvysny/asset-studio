extends RefCounted
# Strict AssetDescriptorV1 parser (asset-descriptor.schema.json). Parses RAW bytes and hashes the raw
# bytes; the document is never re-serialized. forward_axis is "+Z" (docs/adr/0001-descriptor-forward-axis.md).

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")
const AssetRef = preload("res://addons/assetstudio/core/as_asset_ref.gd")
const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")

const REQUIRED: PackedStringArray = [
	"schema_version", "asset_ref", "kind", "units", "up_axis", "forward_axis", "bounds_min", "bounds_max",
	"placement_anchor", "footprint_radius_m", "scale_range", "height_offset_range_m", "default_grounding",
	"material_slots", "collision", "preview_warnings", "source_provenance", "licence",
]
const SURFACE_KEYS: Dictionary = {
	"portable_glb_v1": ["mesh", "primitive"],
	"mobile_glb_v1": ["mesh", "primitive"],
	"godot_static_source_v1": ["node_path", "surface"],
}
const SHAPE_TYPES: PackedStringArray = ["box", "sphere", "capsule", "cylinder", "convex", "concave"]
const GROUNDINGS: PackedStringArray = ["FOLLOW_TERRAIN", "WORLD_FIXED"]

var raw_sha256: String = ""
var asset_ref: RefCounted = null
var data: Dictionary = {}


## Returns an ASResult whose value is an ASAssetDescriptor (raw_sha256 = sha256 of `raw`).
static func parse_bytes(raw: PackedByteArray) -> RefCounted:
	var parsed: Dictionary = Schema.parse_json_bytes(raw)
	if not parsed["ok"]:
		return Result.fail(Result.CODE_INVALID_RESPONSE, "descriptor: %s" % parsed["error"])
	var d: Dictionary = parsed["value"]
	var err: String = validate(d)
	if err != "":
		return Result.fail(Result.CODE_INVALID_RESPONSE, "descriptor: %s" % err)
	var desc: RefCounted = new()
	desc.set("raw_sha256", Canonical.sha256_hex(raw))
	desc.set("data", d)
	desc.set("asset_ref", AssetRef.parse(d["asset_ref"]).value)
	return Result.success(desc)


static func validate(d: Dictionary) -> String:
	var err: String = Schema.check_keys(d, REQUIRED, PackedStringArray(), "descriptor")
	if err != "":
		return err
	err = _check_constants(d)
	if err == "":
		err = AssetRef.validate(d["asset_ref"])
	if err == "":
		err = _check_geometry(d)
	if err == "":
		err = _check_slots(d["material_slots"])
	if err == "":
		err = _check_collision(d["collision"])
	if err == "":
		err = _check_free_form(d)
	return err


static func _check_constants(d: Dictionary) -> String:
	if Schema.check_int(d["schema_version"], 1, 1, "schema_version") != "":
		return "schema_version must be 1"
	var consts: Dictionary = {"kind": "model3d", "units": "m", "up_axis": "+Y", "forward_axis": "+Z"}
	for k: String in consts:
		if d[k] != consts[k]:
			return "%s must be %s" % [k, consts[k]]
	if not d["default_grounding"] is String or not GROUNDINGS.has(d["default_grounding"]):
		return "default_grounding: unknown value"
	return ""


static func _check_geometry(d: Dictionary) -> String:
	for k: String in ["bounds_min", "bounds_max", "placement_anchor"]:
		var err: String = Schema.check_decimal_array(d[k], 3, false, k)
		if err != "":
			return err
	for pair: Array in [["scale_range", true], ["height_offset_range_m", false]]:
		var f: String = pair[0]
		var err: String = Schema.check_decimal_array(d[f], 2, pair[1], f)
		if err != "":
			return err
		if Schema.micro(d[f][0]) > Schema.micro(d[f][1]):
			return "%s: min exceeds max" % f
	var err2: String = Schema.check_positive_decimal(d["footprint_radius_m"], "footprint_radius_m")
	if err2 != "":
		return err2
	var has_extent: bool = false
	for i: int in 3:
		var lo: int = Schema.micro(d["bounds_min"][i])
		var hi: int = Schema.micro(d["bounds_max"][i])
		if lo > hi:
			return "bounds_min exceeds bounds_max"
		has_extent = has_extent or hi > lo
	return "" if has_extent else "bounds have no extent"


static func _check_slots(slots: Variant) -> String:
	if not slots is Array or (slots as Array).size() > 64:
		return "material_slots: expected array of at most 64"
	var seen: Dictionary = {}
	for s: Variant in slots:
		if not s is Dictionary:
			return "material_slots: expected objects"
		var slot: Dictionary = s
		var err: String = Schema.check_keys(slot, ["slot_id", "role", "surfaces"], PackedStringArray(), "material_slot")
		if err == "":
			err = Schema.check_pattern(slot["slot_id"], "slug", "slot_id")
		if err == "":
			err = Schema.check_pattern(slot["role"], "slug", "role")
		if err == "":
			err = _check_surfaces(slot["surfaces"])
		if err != "":
			return err
		if seen.has(slot["slot_id"]):
			return "duplicate slot_id %s" % slot["slot_id"]
		seen[slot["slot_id"]] = true
	return ""


static func _check_surfaces(surfaces: Variant) -> String:
	if not surfaces is Dictionary or (surfaces as Dictionary).is_empty():
		return "surfaces: expected non-empty object"
	for k: Variant in (surfaces as Dictionary).keys():
		if not k is String or not SURFACE_KEYS.has(k):
			return "surfaces: unknown representation %s" % str(k)
		var items: Variant = surfaces[k]
		if not items is Array or (items as Array).is_empty() or (items as Array).size() > 1024:
			return "surfaces.%s: expected 1..1024 items" % k
		var keys: Array = SURFACE_KEYS[k]
		for item: Variant in items:
			var err: String = _check_surface_item(item, keys)
			if err != "":
				return err
	return ""


static func _check_surface_item(item: Variant, keys: Array) -> String:
	if not item is Dictionary:
		return "surface: expected object"
	var dict: Dictionary = item
	var err: String = Schema.check_keys(dict, PackedStringArray(keys), PackedStringArray(), "surface")
	if err != "":
		return err
	for k: String in keys:
		err = Schema.check_pattern(dict[k], "node_path", k) if k == "node_path" else Schema.check_int(dict[k], 0, 2147483647, k)
		if err != "":
			return err
	return ""


static func _check_collision(c: Variant) -> String:
	if c == null:
		return ""
	if not c is Dictionary:
		return "collision: expected object or null"
	var dict: Dictionary = c
	var err: String = Schema.check_keys(dict, ["source", "shape_count", "shape_types"], PackedStringArray(), "collision")
	if err != "":
		return err
	if dict["source"] != "godot_static_source_v1":
		return "collision.source: unsupported"
	err = Schema.check_int(dict["shape_count"], 1, 1024, "shape_count")
	if err != "":
		return err
	var types: Variant = dict["shape_types"]
	if not types is Array or (types as Array).is_empty() or (types as Array).size() > 6:
		return "shape_types: expected 1..6 items"
	var seen: Dictionary = {}
	for t: Variant in types:
		if not t is String or not SHAPE_TYPES.has(t) or seen.has(t):
			return "shape_types: unknown or duplicate"
		seen[t] = true
	return ""


static func _check_free_form(d: Dictionary) -> String:
	var warnings: Variant = d["preview_warnings"]
	if not warnings is Array or (warnings as Array).size() > 64:
		return "preview_warnings: expected array of at most 64"
	for w: Variant in warnings:
		var err: String = Schema.check_pattern(w, "slug", "preview_warnings")
		if err != "":
			return err
	for k: String in ["source_provenance", "licence"]:
		if not d[k] is Dictionary:
			return "%s: expected object" % k
		var err: String = Schema.check_json_value(d[k], k)
		if err != "":
			return err
	return ""
