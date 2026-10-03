@tool
extends RefCounted
# ZIP container layer of the source-package validator (static-source-package.md §1). The central directory is
# parsed here (Godot's ZIPReader hides flags, methods and attributes); member bytes are then read through
# ZIPReader, which bounds every member by its declared size. Nothing is written anywhere.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Policy = preload("res://addons/assetstudio/project/as_srcpkg_policy.gd")

const EOCD_SIZE: int = 22
const CD_ENTRY_SIZE: int = 46
const MAX_CENTRAL_DIR_BYTES: int = 64 * 1024 * 1024

## name -> {"size", "csize", "crc", "offset", "method"} for every regular-file member (directories are dropped).
var members: Dictionary = {}

var _reader: ZIPReader = null


static func unsafe(detail: String, message: String, path: String = "") -> RefCounted:
	return Result.fail("unsafe_package", message, false, {"detail": detail, "path": path})


static func limit(detail: String, message: String, path: String = "") -> RefCounted:
	return Result.fail("resource_limit", message, false, {"detail": detail, "path": path})


## value = ASSourceZip (call close() when done).
static func open(path: String) -> RefCounted:
	var z: RefCounted = new()
	var scanned: RefCounted = z.call("_scan", path)
	if not scanned.ok:
		return scanned
	var reader := ZIPReader.new()
	if reader.open(path) != OK:
		return unsafe("bad_zip", "cannot open the archive")
	z.set("_reader", reader)
	var names: PackedStringArray = reader.get_files()
	for name: String in (z.get("members") as Dictionary):
		if not names.has(name):
			z.call("close")
			return unsafe("bad_zip", "member is not readable through the archive index", name)
	return Result.success(z)


func close() -> void:
	if _reader != null:
		_reader.close()
		_reader = null


## value = the member's bytes; their count equals the declared size (ZIPReader never returns more).
func read(name: String) -> RefCounted:
	if _reader == null or not members.has(name):
		return unsafe("bad_zip", "no such member", name)
	var data: PackedByteArray = _reader.read_file(name)
	if data.size() != int(members[name]["size"]):
		return unsafe("bad_zip", "member size disagrees with its header", name)
	return Result.success(data)


func _scan(path: String) -> RefCounted:
	var f: FileAccess = FileAccess.open(path, FileAccess.READ)
	if f == null:
		return Result.fail(Result.CODE_IO_ERROR, "cannot open the archive")
	var size: int = f.get_length()
	if size > Policy.MAX_UPLOAD_BYTES:
		return limit("upload_size", "archive larger than %d bytes" % Policy.MAX_UPLOAD_BYTES)
	var eocd: Dictionary = _read_eocd(f, size)
	if eocd.has("error"):
		return eocd["error"]
	var dir: RefCounted = _read_directory(f, size, eocd)
	if not dir.ok:
		return dir
	var parsed: RefCounted = _parse_entries(dir.value, int(eocd["total"]))
	if not parsed.ok:
		return parsed
	return _check_local_headers(f, size)


## {"total", "cd_size", "cd_off"} or {"error": ASResult}.
static func _read_eocd(f: FileAccess, size: int) -> Dictionary:
	if size < EOCD_SIZE:
		return {"error": unsafe("bad_zip", "not a ZIP archive: too small")}
	var tail_len: int = mini(size, EOCD_SIZE + 0xFFFF)
	f.seek(size - tail_len)
	var tail: PackedByteArray = f.get_buffer(tail_len)
	var pos: int = -1
	for i: int in range(tail.size() - EOCD_SIZE, -1, -1):
		if tail[i] == 0x50 and tail[i + 1] == 0x4b and tail[i + 2] == 5 and tail[i + 3] == 6:
			pos = i
			break
	if pos < 0:
		return {"error": unsafe("bad_zip", "not a ZIP archive: no end-of-central-directory record")}
	var total: int = tail.decode_u16(pos + 10)
	var cd_size: int = tail.decode_u32(pos + 12)
	var cd_off: int = tail.decode_u32(pos + 16)
	if pos + EOCD_SIZE + tail.decode_u16(pos + 20) != tail.size():
		return {"error": unsafe("trailing_data", "data after the end-of-central-directory record")}
	if tail.decode_u16(pos + 4) != 0 or tail.decode_u16(pos + 6) != 0 or tail.decode_u16(pos + 8) != total:
		return {"error": unsafe("multi_disk", "multi-disk archives are not accepted")}
	if total == 0xFFFF or cd_size == 0xFFFFFFFF or cd_off == 0xFFFFFFFF:
		return {"error": unsafe("zip64", "ZIP64 archives are not accepted")}
	if total > Policy.MAX_FILES:
		return {"error": limit("too_many_files", "%d members exceeds the limit of %d" % [total, Policy.MAX_FILES])}
	if cd_size > MAX_CENTRAL_DIR_BYTES or cd_off + cd_size > size:
		return {"error": unsafe("bad_zip", "central directory is out of bounds")}
	return {"total": total, "cd_size": cd_size, "cd_off": cd_off}


static func _read_directory(f: FileAccess, _size: int, eocd: Dictionary) -> RefCounted:
	f.seek(int(eocd["cd_off"]))
	var buf: PackedByteArray = f.get_buffer(int(eocd["cd_size"]))
	if buf.size() != int(eocd["cd_size"]):
		return unsafe("bad_zip", "truncated central directory")
	return Result.success(buf)


func _parse_entries(buf: PackedByteArray, total: int) -> RefCounted:
	var p: int = 0
	var folded: Dictionary = {}
	var declared_total: int = 0
	for _i: int in total:
		if p + CD_ENTRY_SIZE > buf.size() or buf.decode_u32(p) != 0x02014b50:
			return unsafe("bad_zip", "bad central directory entry")
		var nlen: int = buf.decode_u16(p + 28)
		var skip: int = nlen + buf.decode_u16(p + 30) + buf.decode_u16(p + 32)
		if p + CD_ENTRY_SIZE + skip > buf.size():
			return unsafe("bad_zip", "central directory entry runs past the directory")
		var raw_name: PackedByteArray = buf.slice(p + CD_ENTRY_SIZE, p + CD_ENTRY_SIZE + nlen)
		var added: RefCounted = _add_member(buf, p, raw_name, folded)
		if not added.ok:
			return added
		declared_total += int(added.value)
		if declared_total > Policy.MAX_EXPANDED_BYTES:
			return limit("expanded_size", "declared expanded size exceeds the limit")
		p += CD_ENTRY_SIZE + skip
	if p != buf.size():
		return unsafe("bad_zip", "central directory entry count disagrees with the end record")
	return Result.success()


## Checks one central directory entry (same order as the server validator); value = declared size.
func _add_member(buf: PackedByteArray, p: int, raw_name: PackedByteArray, folded: Dictionary) -> RefCounted:
	var flags: int = buf.decode_u16(p + 8)
	var method: int = buf.decode_u16(p + 10)
	var csize: int = buf.decode_u32(p + 20)
	var usize: int = buf.decode_u32(p + 24)
	var named: RefCounted = _decode_name(raw_name)
	if not named.ok:
		return named
	var name: String = named.value
	if flags & 1 != 0:
		return unsafe("encrypted", "encrypted member", name)
	if method != 0 and method != 8:
		return unsafe("compression_method", "compression method %d is not allowed" % method, name)
	if name.ends_with("/"):
		if usize != 0 or csize > 2:
			return unsafe("directory_with_content", "directory entry carries content", name)
		return Result.success(0)
	var bad: RefCounted = validate_name(name)
	if not bad.ok:
		return bad
	var kind: int = (buf.decode_u32(p + 38) >> 16) & 0xF000
	if kind == 0xA000:
		return unsafe("symlink", "symbolic link member", name)
	if kind != 0 and kind != 0x8000:
		return unsafe("special_file", "non-regular file member", name)
	if usize > Policy.RATIO_MIN_BYTES and float(usize) / float(maxi(csize, 1)) > Policy.RATIO_LIMIT:
		return limit("compression_ratio", "compression ratio above %d" % Policy.RATIO_LIMIT, name)
	if folded.has(name.to_lower()):
		return unsafe("casefold_collision", "%s collides with another member case-insensitively" % name, name)
	folded[name.to_lower()] = true
	members[name] = {"size": usize, "csize": csize, "crc": buf.decode_u32(p + 16), "offset": buf.decode_u32(p + 42),
			"method": method}
	return Result.success(usize)


## Control characters are rejected on the raw bytes; non-ASCII names can never match the safe path pattern
## ([A-Za-z0-9_.-] segments), so Unicode normalisation (NFC) has nothing left to decide.
static func _decode_name(raw: PackedByteArray) -> RefCounted:
	var ascii := PackedByteArray()
	for b: int in raw:
		if b < 0x20 or b == 0x7f:
			return unsafe("control_character", "member name contains a control character")
		ascii.append(b if b < 0x80 else 0x3f)
	var name: String = ascii.get_string_from_ascii()
	if raw.has(0x5c):
		return unsafe("backslash_path", "member name contains a backslash", name)
	for b: int in raw:
		if b >= 0x80:
			return unsafe("invalid_path", "member name is not plain ASCII (outside the safe path pattern)", name)
	return Result.success(name)


## Name rules of §1.4 for a regular-file member name (also used for every other package path).
static func validate_name(name: String) -> RefCounted:
	if name.begins_with("/") or (name.length() >= 2 and name[1] == ":" and name.unicode_at(0) < 128 and _is_letter(name.unicode_at(0))):
		return unsafe("absolute_path", "member path %s is absolute" % name, name)
	var segments: PackedStringArray = name.split("/")
	if segments.has(".."):
		return unsafe("path_traversal", "member path %s escapes the package root" % name, name)
	if segments.has(".") or segments.has(""):
		return unsafe("invalid_path", "member name has an empty or '.' segment", name)
	if segments.size() > Policy.MAX_DEPTH:
		return limit("depth", "member depth %d exceeds %d" % [segments.size(), Policy.MAX_DEPTH], name)
	if name.length() > 255 or not _safe_chars(name):
		return unsafe("invalid_path", "member name outside [A-Za-z0-9_.-] segments", name)
	return Result.success()


static func _is_letter(c: int) -> bool:
	return (c >= 65 and c <= 90) or (c >= 97 and c <= 122)


static func _safe_chars(name: String) -> bool:
	for i: int in name.length():
		var c: int = name.unicode_at(i)
		if not (_is_letter(c) or (c >= 48 and c <= 57) or c == 95 or c == 46 or c == 45 or c == 47):
			return false
	return true


## Every local header must start where the directory says, carry the same name and fit inside the archive.
func _check_local_headers(f: FileAccess, size: int) -> RefCounted:
	for name: String in members:
		var info: Dictionary = members[name]
		var off: int = int(info["offset"])
		if off + 30 > size:
			return unsafe("bad_zip", "bad local file header", name)
		f.seek(off)
		var head: PackedByteArray = f.get_buffer(30)
		if head.decode_u32(0) != 0x04034b50:
			return unsafe("bad_zip", "bad local file header", name)
		var nlen: int = head.decode_u16(26)
		var local_name: PackedByteArray = f.get_buffer(nlen)
		if local_name.get_string_from_utf8() != name:
			return unsafe("bad_zip", "local header name differs from the central directory", name)
		if off + 30 + nlen + head.decode_u16(28) + int(info["csize"]) > size:
			return unsafe("bad_zip", "member data runs past the end of the archive", name)
	return Result.success()
