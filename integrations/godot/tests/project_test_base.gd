extends "res://tests/test_case.gd"
# Shared helpers for the project-side tests (config, lock, coordinator, installer, restore). Not a test file:
# run_tests.gd only loads test_*.gd. Tests call cleanup() last.

const Fs = preload("res://addons/assetstudio/project/as_fs.gd")
const CJson = preload("res://addons/assetstudio/core/as_canonical_json.gd")

var _dirs: Array[String] = []


func tmp_dir(prefix: String) -> String:
	var dir: String = ProjectSettings.globalize_path("user://as_proj_%s_%d_%d" % [prefix, Time.get_ticks_usec(), randi()])
	DirAccess.make_dir_recursive_absolute(dir)
	_dirs.append(dir)
	return dir


func cleanup() -> void:
	for d: String in _dirs:
		Fs.remove_tree(d)


func contracts_dir() -> String:
	var dir: String = OS.get_environment("ASSETSTUDIO_CONTRACTS_DIR")
	if dir == "":
		dir = ProjectSettings.globalize_path("res://").path_join("../../contracts/godot-integration/v1").simplify_path()
	return dir


func fixture(rel: String) -> PackedByteArray:
	return FileAccess.get_file_as_bytes(contracts_dir().path_join("fixtures").path_join(rel))


func fixture_files(rel_dir: String) -> PackedStringArray:
	var out := PackedStringArray()
	for f: String in DirAccess.get_files_at(contracts_dir().path_join("fixtures").path_join(rel_dir)):
		out.append(rel_dir.path_join(f))
	out.sort()
	return out


func write_text(path: String, text: String) -> void:
	Fs.write_atomic(path, text.to_utf8_buffer())


func read_text(path: String) -> String:
	return Fs.read_bytes(path).get_string_from_utf8()


## relative path -> sha256 for every file under `dir` (hidden entries included), skipping top-level names in `skip`.
## Empty directories are not part of the state.
func snapshot(dir: String, skip: PackedStringArray = PackedStringArray()) -> Dictionary:
	var out: Dictionary = {}
	_walk(dir, "", skip, out)
	return out


func _walk(base: String, rel: String, skip: PackedStringArray, out: Dictionary) -> void:
	var listing: Dictionary = Fs.list_dir(base.path_join(rel) if rel != "" else base)
	for d: String in listing["dirs"]:
		if rel == "" and skip.has(d):
			continue
		_walk(base, rel.path_join(d) if rel != "" else d, skip, out)
	for f: String in listing["files"]:
		var p: String = rel.path_join(f) if rel != "" else f
		out[p] = Fs.sha256_file(base.path_join(p))
