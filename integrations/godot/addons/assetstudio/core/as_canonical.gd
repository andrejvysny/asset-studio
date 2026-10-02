@tool
extends RefCounted
# No class_name: addon scripts are loaded via preload consts so they cannot collide with
# global classes in consumer projects.

const DECIMAL_PLACES: int = 6
const _DECIMAL_PATTERN: String = "^-?(0|[1-9][0-9]*)(\\.[0-9]*[1-9])?$"

static var _decimal_re: RegEx = null


## uint32 little-endian UTF-8 byte length + bytes, per part.
static func length_prefixed(parts: PackedStringArray) -> PackedByteArray:
	var out := PackedByteArray()
	for part: String in parts:
		var bytes: PackedByteArray = part.to_utf8_buffer()
		var n: int = bytes.size()
		out.append(n & 0xFF)
		out.append((n >> 8) & 0xFF)
		out.append((n >> 16) & 0xFF)
		out.append((n >> 24) & 0xFF)
		out.append_array(bytes)
	return out


## Callers pass the raw bytes the server stored; never hash a re-serialized dictionary.
static func sha256_hex(data: PackedByteArray) -> String:
	var ctx := HashingContext.new()
	ctx.start(HashingContext.HASH_SHA256)
	ctx.update(data)
	return ctx.finish().hex_encode()


static func asset_key(server_id: String, library_id: String, asset_id: String, version_id: String) -> String:
	return sha256_hex(length_prefixed(PackedStringArray([server_id, library_id, asset_id, version_id])))


## Returns {"ok": bool, "value": float, "error": String}.
static func parse_decimal(text: String) -> Dictionary:
	if _decimal_re == null:
		_decimal_re = RegEx.create_from_string(_DECIMAL_PATTERN)
	var m: RegExMatch = _decimal_re.search(text)
	# Full-string comparison: PCRE "$" also matches before a trailing newline.
	if m == null or m.get_string() != text or text == "-0":
		return {"ok": false, "value": 0.0, "error": "not a canonical decimal string: %s" % text}
	var frac: String = text.get_slice(".", 1) if text.contains(".") else ""
	if frac.length() > DECIMAL_PLACES:
		return {"ok": false, "value": 0.0, "error": "more than %d fractional digits: %s" % [DECIMAL_PLACES, text]}
	return {"ok": true, "value": text.to_float(), "error": ""}


static func is_canonical_decimal(text: String) -> bool:
	return bool(parse_decimal(text)["ok"])
