@tool
extends RefCounted
# Pure HTTP helpers for ASLibraryClient (no state, no I/O).

const Result = preload("res://addons/assetstudio/core/as_errors.gd")


## Maps a non-2xx response to a typed error using the contract envelope, else by status.
static func http_error(status: int, body: PackedByteArray) -> RefCounted:
	var json := JSON.new()
	if json.parse(body.get_string_from_utf8()) == OK and json.data is Dictionary and (json.data as Dictionary).get("error") is Dictionary:
		var e: Dictionary = (json.data as Dictionary)["error"]
		var code: String = str(e.get("code", ""))
		if Result.is_known_server_code(code):
			var details: Variant = e.get("details", {})
			return Result.fail(code, str(e.get("message", "")).left(1024), bool(e.get("retryable", false)),
					details if details is Dictionary else {})
		return Result.fail(Result.CODE_INVALID_RESPONSE, "unknown error code from server (HTTP %d)" % status)
	if status == 401 or status == 403:
		return Result.fail("unauthorized" if status == 401 else "forbidden", "HTTP %d" % status)
	if status >= 500:
		return Result.fail("temporarily_unavailable", "HTTP %d" % status, true)
	return Result.fail(Result.CODE_INVALID_RESPONSE, "unexpected HTTP %d" % status)


static func header_dict(raw: PackedStringArray) -> Dictionary:
	var out: Dictionary = {}
	for line: String in raw:
		var i: int = line.find(":")
		if i > 0:
			out[line.substr(0, i).strip_edges().to_lower()] = line.substr(i + 1).strip_edges()
	return out


static func query_string(query: Dictionary) -> String:
	var parts := PackedStringArray()
	for k: String in query:
		parts.append("%s=%s" % [k.uri_encode(), str(query[k]).uri_encode()])
	return "" if parts.is_empty() else "?" + "&".join(parts)
