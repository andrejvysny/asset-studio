@tool
extends RefCounted
# Absolute-filesystem helpers for the project-side modules. Everything here takes absolute paths so the same
# code runs from the CLI, headless tests and the editor. Runtime-safe: nothing here needs the editor.

const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")


static func exists(path: String) -> bool:
	return FileAccess.file_exists(path) or DirAccess.dir_exists_absolute(path)


static func read_bytes(path: String) -> PackedByteArray:
	return FileAccess.get_file_as_bytes(path) if FileAccess.file_exists(path) else PackedByteArray()


static func sha256_bytes(data: PackedByteArray) -> String:
	return Canonical.sha256_hex(data)


## sha256 of the file, or "" when it does not exist.
static func sha256_file(path: String) -> String:
	return FileAccess.get_sha256(path) if FileAccess.file_exists(path) else ""


## Write via tmp + rename so readers never see a partial file. Returns OK or an Error.
static func write_atomic(path: String, data: PackedByteArray) -> int:
	var err: int = DirAccess.make_dir_recursive_absolute(path.get_base_dir())
	if err != OK:
		return err
	var tmp: String = path + ".tmp"
	var f: FileAccess = FileAccess.open(tmp, FileAccess.WRITE)
	if f == null:
		return FileAccess.get_open_error()
	f.store_buffer(data)
	f.close()
	return DirAccess.rename_absolute(tmp, path)


static func copy_file(src: String, dst: String) -> int:
	var err: int = DirAccess.make_dir_recursive_absolute(dst.get_base_dir())
	if err != OK:
		return err
	return DirAccess.copy_absolute(src, dst)


## Copies via a tmp name and renames, so an interrupted copy never leaves a plausible-looking file.
static func copy_file_atomic(src: String, dst: String) -> int:
	var tmp: String = dst + ".tmp"
	var err: int = copy_file(src, tmp)
	if err != OK:
		return err
	return DirAccess.rename_absolute(tmp, dst)


## Removes a file or a whole tree, hidden entries included. Missing paths are fine.
static func remove_tree(path: String) -> void:
	if FileAccess.file_exists(path):
		DirAccess.remove_absolute(path)
		return
	var da: DirAccess = DirAccess.open(path)
	if da == null:
		return
	da.include_hidden = true
	for sub: String in da.get_directories():
		remove_tree(path.path_join(sub))
	for f: String in da.get_files():
		DirAccess.remove_absolute(path.path_join(f))
	DirAccess.remove_absolute(path)


## Lists immediate entries (hidden included), sorted. Returns {"dirs": [...], "files": [...]}.
static func list_dir(path: String) -> Dictionary:
	var out: Dictionary = {"dirs": PackedStringArray(), "files": PackedStringArray()}
	var da: DirAccess = DirAccess.open(path)
	if da == null:
		return out
	da.include_hidden = true
	var dirs: PackedStringArray = da.get_directories()
	var files: PackedStringArray = da.get_files()
	dirs.sort()
	files.sort()
	out["dirs"] = dirs
	out["files"] = files
	return out


## "res://a/b" -> "<root>/a/b". `root` is the absolute project directory.
static func res_to_abs(root: String, res_path: String) -> String:
	return root.path_join(res_path.trim_prefix("res://"))


static func is_safe_res_path(p: String) -> bool:
	if not p.begins_with("res://") or p.contains("\\") or p.substr(6).contains(":"):
		return false
	for seg: String in p.substr(6).split("/"):
		if seg.is_empty() or seg == "." or seg == "..":
			return false
	return true
