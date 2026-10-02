@tool
extends RefCounted
# Strict validation primitives shared by the value parsers. Each check returns "" when valid, else a reason.
# Mirrors contracts/godot-integration/v1/asset-ref.schema.json $defs.

const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")

const PATTERNS: Dictionary = {
	"server_id": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
	"library_id": "^prj_[0-9a-hjkmnp-tv-z]{16}$",
	"asset_id": "^ast_[0-9a-hjkmnp-tv-z]{16}$",
	"version_id": "^ver_[0-9a-hjkmnp-tv-z]{16}$",
	"artifact_id": "^art_[0-9a-hjkmnp-tv-z]{16}$",
	"delivery_id": "^dlv_[0-9a-hjkmnp-tv-z]{16}$",
	"slug": "^[a-z0-9][a-z0-9_.-]{0,63}$",
	"version_text": "^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$",
	"sha256": "^[0-9a-f]{64}$",
	"media_type": "^[a-z0-9]+/[a-z0-9.+-]+$",
	"node_path": "^(\\.|[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*)$",
	"path_segment": "^[A-Za-z0-9_.-]+$",
	"url_id": "^[A-Za-z0-9_.-]+$",
}
const REPRESENTATIONS: PackedStringArray = ["portable_glb_v1", "godot_static_source_v1", "mobile_glb_v1"]
const CAPABILITIES: PackedStringArray = [
	"godot_text_scene_v1", "csg_static", "static_collision", "shader_source", "vertex_colors", "alpha_mask",
	"alpha_blend", "pbr_textures",
]
const MAX_PATH_LEN: int = 255
const MAX_PATH_DEPTH: int = 32
const MAX_JSON_DEPTH: int = 16

static var _regexes: Dictionary = {}


## Full-string match: PCRE "$" also matches before a trailing newline, so compare the match to the input.
static func matches(pattern_name: String, text: String) -> bool:
	if not _regexes.has(pattern_name):
		_regexes[pattern_name] = RegEx.create_from_string(PATTERNS[pattern_name])
	var re: RegEx = _regexes[pattern_name]
	var m: RegExMatch = re.search(text)
	return m != null and m.get_string() == text


static func check_pattern(v: Variant, pattern_name: String, field: String) -> String:
	if not v is String:
		return "%s: expected string" % field
	if not matches(pattern_name, v):
		return "%s: invalid %s" % [field, pattern_name]
	return ""


## Rejects absolute paths, "." / ".." segments, backslashes and empty segments (all fail the segment pattern).
static func check_safe_path(v: Variant, field: String) -> String:
	if not v is String:
		return "%s: expected string" % field
	var path: String = v
	if path.is_empty() or path.length() > MAX_PATH_LEN:
		return "%s: bad path length" % field
	var segments: PackedStringArray = path.split("/")
	if segments.size() > MAX_PATH_DEPTH:
		return "%s: path too deep" % field
	for seg: String in segments:
		if seg == "." or seg == ".." or not matches("path_segment", seg):
			return "%s: unsafe path" % field
	return ""


static func check_keys(d: Dictionary, required: PackedStringArray, optional: PackedStringArray, field: String) -> String:
	for k: Variant in d.keys():
		if not k is String or not (required.has(k) or optional.has(k)):
			return "%s: unknown field %s" % [field, str(k)]
	for k: String in required:
		if not d.has(k):
			return "%s: missing field %s" % [field, k]
	return ""


## JSON numbers arrive as floats in GDScript; accept only integral values within [lo, hi].
static func check_int(v: Variant, lo: int, hi: int, field: String) -> String:
	if not (v is float or v is int):
		return "%s: expected integer" % field
	var f: float = float(v)
	if f != floorf(f) or f < lo or f > hi:
		return "%s: integer out of range" % field
	return ""


static func check_decimal(v: Variant, field: String) -> String:
	if not v is String or not Canonical.is_canonical_decimal(v):
		return "%s: not a canonical decimal string" % field
	return ""


static func check_positive_decimal(v: Variant, field: String) -> String:
	var err: String = check_decimal(v, field)
	if err != "":
		return err
	if micro(v) <= 0:
		return "%s: must be positive" % field
	return ""


static func check_decimal_array(v: Variant, n: int, positive: bool, field: String) -> String:
	if not v is Array or (v as Array).size() != n:
		return "%s: expected array of %d decimals" % [field, n]
	for item: Variant in v:
		var err: String = check_positive_decimal(item, field) if positive else check_decimal(item, field)
		if err != "":
			return err
	return ""


## Exact fixed-point value (1e-6 units) of a canonical decimal string; avoids float comparison.
static func micro(text: String) -> int:
	var neg: bool = text.begins_with("-")
	var body: String = text.substr(1) if neg else text
	var whole: String = body.get_slice(".", 0)
	var frac: String = body.get_slice(".", 1) if body.contains(".") else ""
	while frac.length() < Canonical.DECIMAL_PLACES:
		frac += "0"
	var n: int = int(whole) * 1000000 + int(frac)
	return -n if neg else n


## schema json_value: no non-integral numbers anywhere (decimals must be strings).
static func check_json_value(v: Variant, field: String, depth: int = 0) -> String:
	if depth > MAX_JSON_DEPTH:
		return "%s: nesting too deep" % field
	match typeof(v):
		TYPE_NIL, TYPE_BOOL, TYPE_STRING:
			return ""
		TYPE_INT:
			return ""
		TYPE_FLOAT:
			var f: float = v
			return "" if f == floorf(f) and absf(f) < 9.0e15 else "%s: float literal not allowed" % field
		TYPE_ARRAY:
			for item: Variant in v:
				var err: String = check_json_value(item, field, depth + 1)
				if err != "":
					return err
			return ""
		TYPE_DICTIONARY:
			var d: Dictionary = v
			for k: Variant in d.keys():
				if not k is String:
					return "%s: non-string key" % field
				var err: String = check_json_value(d[k], field, depth + 1)
				if err != "":
					return err
			return ""
	return "%s: unsupported value" % field


## Parses strict UTF-8 JSON from raw bytes. Returns {"ok", "value", "error"}.
static func parse_json_bytes(raw: PackedByteArray, max_bytes: int = 4194304) -> Dictionary:
	if raw.is_empty() or raw.size() > max_bytes:
		return {"ok": false, "value": null, "error": "empty or oversized JSON document"}
	var text: String = raw.get_string_from_utf8()
	if text.to_utf8_buffer() != raw:
		return {"ok": false, "value": null, "error": "invalid UTF-8"}
	var json := JSON.new()
	if json.parse(text) != OK:
		return {"ok": false, "value": null, "error": "invalid JSON: %s" % json.get_error_message()}
	if not json.data is Dictionary:
		return {"ok": false, "value": null, "error": "expected a JSON object"}
	return {"ok": true, "value": json.data, "error": ""}
