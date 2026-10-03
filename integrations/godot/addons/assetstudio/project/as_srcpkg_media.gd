@tool
extends RefCounted
# Binary member checks of a source package: image headers and self-contained static GLBs (spec §5). Headers
# only; pixels and meshes are never decoded here.

const Zip = preload("res://addons/assetstudio/project/as_srcpkg_zip.gd")
const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Policy = preload("res://addons/assetstudio/project/as_srcpkg_policy.gd")

const MAX_SIDE: int = 16384
const MAX_PIXEL_BYTES: int = 256 * 1024 * 1024
const MAX_GLB_BYTES: int = 512 * 1024 * 1024
const MAX_JSON_BYTES: int = 16 * 1024 * 1024
const PNG_MAGIC: PackedByteArray = [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]


static func _bad(detail: String, message: String, path: String) -> RefCounted:
	return Zip.unsafe(detail, message, path)


static func _be16(d: PackedByteArray, i: int) -> int:
	return (d[i] << 8) | d[i + 1]


static func _be32(d: PackedByteArray, i: int) -> int:
	return (d[i] << 24) | (d[i + 1] << 16) | (d[i + 2] << 8) | d[i + 3]


## suffix is ".png", ".jpg", ".jpeg" or ".webp".
static func check_image(path: String, data: PackedByteArray, suffix: String) -> RefCounted:
	var size: Variant = null
	match suffix:
		".png":
			size = _png_size(data)
		".webp":
			size = _webp_size(data)
		_:
			size = _jpeg_size(data)
	if size is String:
		return _bad("image_invalid", "unreadable %s header: %s" % [suffix, size], path)
	if size == null:
		return _bad("image_magic_mismatch", "content is not %s data" % suffix.trim_prefix("."), path)
	var w: int = size[0]
	var h: int = size[1]
	if w < 1 or h < 1:
		return _bad("image_invalid", "image has zero size", path)
	if w > MAX_SIDE or h > MAX_SIDE or w * h * 4 > MAX_PIXEL_BYTES:
		return Zip.limit("image_dimensions", "image %dx%d exceeds the decode budget" % [w, h], path)
	return Result.success()


## [w, h], null (wrong magic) or an error String.
static func _png_size(d: PackedByteArray) -> Variant:
	if d.size() < 8 or d.slice(0, 8) != PNG_MAGIC:
		return null
	if d.size() < 24 or d.slice(12, 16).get_string_from_ascii() != "IHDR":
		return "missing IHDR"
	return [_be32(d, 16), _be32(d, 20)]


static func _jpeg_size(d: PackedByteArray) -> Variant:
	if d.size() < 3 or d[0] != 0xFF or d[1] != 0xD8 or d[2] != 0xFF:
		return null
	var i: int = 2
	while i + 4 <= d.size():
		if d[i] != 0xFF:
			return "bad marker"
		var marker: int = d[i + 1]
		if marker == 0xFF:
			i += 1
		elif marker == 0x01 or (marker >= 0xD0 and marker <= 0xD9):
			i += 2
		elif marker >= 0xC0 and marker <= 0xCF and marker != 0xC4 and marker != 0xC8 and marker != 0xCC:
			if i + 9 > d.size():
				break
			return [_be16(d, i + 7), _be16(d, i + 5)]
		else:
			i += 2 + _be16(d, i + 2)
	return "no frame header"


static func _webp_size(d: PackedByteArray) -> Variant:
	if d.size() < 12 or d.slice(0, 4).get_string_from_ascii() != "RIFF" or d.slice(8, 12).get_string_from_ascii() != "WEBP":
		return null
	var kind: String = d.slice(12, 16).get_string_from_ascii() if d.size() >= 16 else ""
	if kind == "VP8 " and d.size() >= 30 and d[23] == 0x9d and d[24] == 0x01 and d[25] == 0x2a:
		return [d.decode_u16(26) & 0x3FFF, d.decode_u16(28) & 0x3FFF]
	if kind == "VP8L" and d.size() >= 25 and d[20] == 0x2F:
		var bits: int = d.decode_u32(21)
		return [(bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1]
	if kind == "VP8X" and d.size() >= 30:
		return [(d[24] | (d[25] << 8) | (d[26] << 16)) + 1, (d[27] | (d[28] << 8) | (d[29] << 16)) + 1]
	return "unsupported WebP layout"


## Container + JSON chunk checks: self-contained (no external uri), no skins, no animations, only allowed
## required extensions. A .glb extension alone proves nothing, so the JSON chunk is parsed.
static func check_glb(path: String, data: PackedByteArray) -> RefCounted:
	if data.size() > MAX_GLB_BYTES:
		return Zip.limit("glb_size", "GLB larger than %d bytes" % MAX_GLB_BYTES, path)
	if data.size() < 20 or data.slice(0, 4).get_string_from_ascii() != "glTF" or data.decode_u32(4) != 2:
		return _bad("glb_invalid", "not a glTF 2.0 binary (GLB)", path)
	if data.decode_u32(8) != data.size():
		return _bad("glb_invalid", "declared length differs from the file size", path)
	var json_len: int = data.decode_u32(12)
	if data.slice(16, 20).get_string_from_ascii() != "JSON" or json_len > MAX_JSON_BYTES or 20 + json_len > data.size():
		return _bad("glb_invalid", "missing or oversized JSON chunk", path)
	var jtext: PackedByteArray = data.slice(20, 20 + json_len)
	var text: String = jtext.get_string_from_utf8()
	var json := JSON.new()
	if text.to_utf8_buffer() != jtext or json.parse(text) != OK or not json.data is Dictionary:
		return _bad("glb_invalid", "invalid JSON chunk", path)
	return _check_document(path, json.data)


static func _check_document(path: String, doc: Dictionary) -> RefCounted:
	for section: String in ["buffers", "images"]:
		var entries: Variant = doc.get(section)
		if entries == null:
			continue
		if not entries is Array:
			return _bad("glb_invalid", "%s must be an array" % section, path)
		for e: Variant in entries:
			if not e is Dictionary:
				return _bad("glb_invalid", "%s entry must be an object" % section, path)
			var uri: Variant = e.get("uri")
			if uri != null and not (uri is String and (uri as String).begins_with("data:")):
				return _bad("glb_external_uri", "%s has an external URI reference" % section, path)
	for what: String in ["skins", "animations"]:
		if doc.get(what) is Array and not (doc[what] as Array).is_empty():
			return _bad("glb_not_static", "%s present: only static models are accepted" % what, path)
	var required: Variant = doc.get("extensionsRequired")
	if required != null and not required is Array:
		return _bad("glb_invalid", "extensionsRequired must be an array", path)
	for ext: Variant in (required if required != null else []):
		if not ext is String or not Policy.ALLOWED_REQUIRED_EXTENSIONS.has(ext):
			return _bad("glb_not_static", "required extension %s is not supported" % str(ext), path)
	return Result.success()
