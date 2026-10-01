extends RefCounted
# Canonical JSON writer, byte-identical to Python json.dumps(sort_keys=True, separators=(",", ":"),
# ensure_ascii=False). Deliberately not JSON.stringify: key order, escapes and number formatting must not depend
# on the engine. Godot parses every JSON number as float, so an integral float with |x| <= 2^53 is written as an
# integer; non-integral floats, NaN and inf are rejected (contract documents carry decimal strings instead).
# Godot strings cannot hold U+0000 or lone surrogates, so those never reach the writer.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")

const MAX_SAFE_INT: float = 9007199254740992.0
const MAX_DEPTH: int = 64


## ASResult whose value is the canonical UTF-8 PackedByteArray.
static func encode(v: Variant) -> RefCounted:
	var out := PackedStringArray()
	var err: String = _emit(v, out, "$", 0)
	if err != "":
		return Result.fail("invalid_request", "canonical json: %s" % err)
	return Result.success("".join(out).to_utf8_buffer())


## True when `raw` is valid UTF-8 JSON that re-encodes to exactly the same bytes.
static func is_canonical(raw: PackedByteArray) -> bool:
	return parse_canonical(raw).ok


## ASResult whose value is the parsed document, only if `raw` is already canonical (duplicate keys, floats like
## 1.0, whitespace, BOMs and unsorted keys all re-encode differently and are rejected).
static func parse_canonical(raw: PackedByteArray) -> RefCounted:
	var parsed: RefCounted = parse_strict_utf8(raw)
	if not parsed.ok:
		return parsed
	var enc: RefCounted = encode(parsed.value)
	if not enc.ok:
		return enc
	if (enc.value as PackedByteArray) != raw:
		return Result.fail(Result.CODE_INVALID_PROJECT_FILE, "not canonical JSON bytes")
	return parsed


## Parses UTF-8 JSON of any shape; does not require canonical form.
static func parse_strict_utf8(raw: PackedByteArray) -> RefCounted:
	var text: String = raw.get_string_from_utf8()
	if text.to_utf8_buffer() != raw:
		return Result.fail(Result.CODE_INVALID_PROJECT_FILE, "invalid UTF-8")
	var json := JSON.new()
	if json.parse(text) != OK:
		return Result.fail(Result.CODE_INVALID_PROJECT_FILE, "invalid JSON: %s" % json.get_error_message())
	return Result.success(json.data)


## Code point order (what Python's str sort uses). Not String.operator<, which is an engine detail.
static func key_less(a: String, b: String) -> bool:
	var n: int = mini(a.length(), b.length())
	for i: int in n:
		var ca: int = a.unicode_at(i)
		var cb: int = b.unicode_at(i)
		if ca != cb:
			return ca < cb
	return a.length() < b.length()


static func _emit(v: Variant, out: PackedStringArray, path: String, depth: int) -> String:
	if depth > MAX_DEPTH:
		return "%s: nesting too deep" % path
	match typeof(v):
		TYPE_NIL:
			out.append("null")
		TYPE_BOOL:
			out.append("true" if v else "false")
		TYPE_INT:
			out.append(str(v))
		TYPE_FLOAT:
			return _emit_float(v, out, path)
		TYPE_STRING, TYPE_STRING_NAME:
			out.append(quote(str(v)))
		TYPE_ARRAY:
			return _emit_array(v, out, path, depth)
		TYPE_DICTIONARY:
			return _emit_dict(v, out, path, depth)
		_:
			return "%s: unsupported value type %d" % [path, typeof(v)]
	return ""


static func _emit_float(f: float, out: PackedStringArray, path: String) -> String:
	if is_nan(f) or is_inf(f) or f != floorf(f) or absf(f) > MAX_SAFE_INT:
		return "%s: non-integral or unsafe number (use a decimal string)" % path
	out.append(str(int(f)))
	return ""


static func _emit_array(a: Array, out: PackedStringArray, path: String, depth: int) -> String:
	out.append("[")
	for i: int in a.size():
		if i > 0:
			out.append(",")
		var err: String = _emit(a[i], out, "%s[%d]" % [path, i], depth + 1)
		if err != "":
			return err
	out.append("]")
	return ""


static func _emit_dict(d: Dictionary, out: PackedStringArray, path: String, depth: int) -> String:
	var keys: Array = d.keys()
	for k: Variant in keys:
		if not (k is String or k is StringName):
			return "%s: object keys must be strings" % path
	var sorted_keys: Array[String] = []
	for k: Variant in keys:
		sorted_keys.append(str(k))
	sorted_keys.sort_custom(key_less)
	out.append("{")
	for i: int in sorted_keys.size():
		if i > 0:
			out.append(",")
		var key: String = sorted_keys[i]
		out.append(quote(key))
		out.append(":")
		var value: Variant = d[key] if d.has(key) else d[StringName(key)]
		var err: String = _emit(value, out, "%s.%s" % [path, key], depth + 1)
		if err != "":
			return err
	out.append("}")
	return ""


## JSON string literal exactly as Python writes it with ensure_ascii=False.
static func quote(s: String) -> String:
	var plain: bool = true
	for i: int in s.length():
		var c: int = s.unicode_at(i)
		if c < 0x20 or c == 0x22 or c == 0x5C:
			plain = false
			break
	if plain:
		return "\"%s\"" % s
	var parts := PackedStringArray(["\""])
	for i: int in s.length():
		parts.append(_escape(s.unicode_at(i)))
	parts.append("\"")
	return "".join(parts)


static func _escape(c: int) -> String:
	match c:
		0x22:
			return "\\\""
		0x5C:
			return "\\\\"
		0x08:
			return "\\b"
		0x09:
			return "\\t"
		0x0A:
			return "\\n"
		0x0C:
			return "\\f"
		0x0D:
			return "\\r"
	if c < 0x20:
		return "\\u%04x" % c
	return String.chr(c)
