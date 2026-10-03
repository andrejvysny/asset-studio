@tool
extends RefCounted
# multipart/form-data body builder for the publication preview upload. Parts come from bytes or are read from
# files in bounded chunks; the total size is checked against the server limit BEFORE any byte is read, so a body
# larger than the limit is never assembled.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")

const CHUNK: int = 1048576
const OVERHEAD_PER_PART: int = 512
const NAME_PATTERN: String = "^[a-z][a-z0-9_]{0,31}$"


## parts: {name: {"bytes": PackedByteArray | "path": String, "filename": String, "media_type": String}}.
## value = {"body": PackedByteArray, "content_type": String, "size": int}.
static func build(parts: Dictionary, max_bytes: int) -> RefCounted:
	var sizes: RefCounted = _sizes(parts, max_bytes)
	if not sizes.ok:
		return sizes
	var boundary: String = "assetstudio-" + Crypto.new().generate_random_bytes(16).hex_encode()
	var body := PackedByteArray()
	var names: Array = parts.keys()
	names.sort()
	for part_name: String in names:
		var spec: Dictionary = parts[part_name]
		body.append_array(_head(boundary, part_name, spec).to_utf8_buffer())
		var added: RefCounted = _append_content(body, spec)
		if not added.ok:
			return added
		body.append_array("\r\n".to_utf8_buffer())
	body.append_array(("--%s--\r\n" % boundary).to_utf8_buffer())
	return Result.success({"body": body, "content_type": "multipart/form-data; boundary=" + boundary,
			"size": body.size()})


static func _sizes(parts: Dictionary, max_bytes: int) -> RefCounted:
	var re: RegEx = RegEx.create_from_string(NAME_PATTERN)
	var total: int = 0
	for part_name: Variant in parts:
		var spec: Variant = parts[part_name]
		if not part_name is String or re.search(part_name) == null or not spec is Dictionary:
			return Result.fail("invalid_request", "invalid multipart part")
		var size: int = _content_size(spec)
		if size < 0:
			return Result.fail(Result.CODE_IO_ERROR, "cannot read part %s" % part_name)
		total += size + OVERHEAD_PER_PART
	if total > max_bytes:
		return Result.fail("resource_limit", "upload of %d bytes exceeds the server limit of %d" % [total, max_bytes],
				false, {"limit": "publication_upload_max_bytes", "max": max_bytes})
	return Result.success(total)


static func _content_size(spec: Dictionary) -> int:
	if spec.has("bytes"):
		return (spec["bytes"] as PackedByteArray).size()
	if not spec.has("path") or not FileAccess.file_exists(spec["path"]):
		return -1
	var f: FileAccess = FileAccess.open(spec["path"], FileAccess.READ)
	return f.get_length() if f != null else -1


static func _head(boundary: String, part_name: String, spec: Dictionary) -> String:
	var filename: String = str(spec.get("filename", part_name)).replace("\"", "_").replace("\r", "_").replace("\n", "_")
	return "--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n" % [
			boundary, part_name, filename, spec.get("media_type", "application/octet-stream")]


static func _append_content(body: PackedByteArray, spec: Dictionary) -> RefCounted:
	if spec.has("bytes"):
		body.append_array(spec["bytes"])
		return Result.success()
	var f: FileAccess = FileAccess.open(spec["path"], FileAccess.READ)
	if f == null:
		return Result.fail(Result.CODE_IO_ERROR, "cannot read %s" % spec["path"])
	while not f.eof_reached():
		var chunk: PackedByteArray = f.get_buffer(CHUNK)
		if chunk.is_empty():
			break
		body.append_array(chunk)
	return Result.success()
