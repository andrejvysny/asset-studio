extends RefCounted
# Verified, immutable content-addressed cache. Layout under `root`:
#   blobs/<sha[0:2]>/<sha>      verified files (never overwritten once verified)
#   staging/                    in-progress downloads
#   manifests/<sha>.json        raw manifest bytes      descriptors/<sha>.json   raw descriptor bytes
#   refs/<asset_key>.json       exact-reference index for offline use
#   pins.json                   owner -> pinned blob shas
# Nothing is ever pruned automatically; prune() is explicit and keeps every pinned blob.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")
const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")

const DEFAULT_ROOT: String = "user://assetstudio/cache"

var root: String = DEFAULT_ROOT
var _pins: Dictionary = {}  # owner_id -> Array[String] of shas


func _init(cache_root: String = DEFAULT_ROOT) -> void:
	root = cache_root
	_load_pins()


func blob_path(sha: String) -> String:
	return root.path_join("blobs").path_join(sha.substr(0, 2)).path_join(sha)


func staging_path(sha: String) -> String:
	return root.path_join("staging").path_join(sha + ".part")


func has_blob(sha: String) -> bool:
	return Schema.matches("sha256", sha) and FileAccess.file_exists(blob_path(sha))


## Cheap presence check plus size; full re-hash is verify_blob().
func has_blob_sized(sha: String, size: int) -> bool:
	if not has_blob(sha):
		return false
	var f: FileAccess = FileAccess.open(blob_path(sha), FileAccess.READ)
	return f != null and f.get_length() == size


func verify_blob(sha: String) -> bool:
	return has_blob(sha) and FileAccess.get_sha256(blob_path(sha)) == sha


## Verifies staging bytes against (sha, size) and atomically moves them into blobs/.
func install_from_staging(staging_file: String, sha: String, size: int) -> RefCounted:
	if not Schema.matches("sha256", sha):
		return Result.fail("invalid_request", "invalid sha256")
	if not FileAccess.file_exists(staging_file):
		return Result.fail("invalid_request", "staging file missing")
	if _file_size(staging_file) != size or FileAccess.get_sha256(staging_file) != sha:
		DirAccess.remove_absolute(staging_file)
		return Result.fail("integrity_mismatch", "staged bytes do not match expected size/sha256")
	var target: String = blob_path(sha)
	if FileAccess.file_exists(target):
		if FileAccess.get_sha256(target) == sha:
			DirAccess.remove_absolute(staging_file)
			return Result.success(target)
		DirAccess.remove_absolute(target)  # corrupt on disk: replace with the freshly verified bytes
	DirAccess.make_dir_recursive_absolute(target.get_base_dir())
	if DirAccess.rename_absolute(staging_file, target) != OK:
		return Result.fail("temporarily_unavailable", "cannot move blob into cache")
	return Result.success(target)


func store_document(kind: String, sha: String, raw: PackedByteArray) -> RefCounted:
	if kind != "manifests" and kind != "descriptors":
		return Result.fail("invalid_request", "unknown document kind")
	if Canonical.sha256_hex(raw) != sha:
		return Result.fail("integrity_mismatch", "document bytes do not match sha256")
	return _write_atomic(root.path_join(kind).path_join(sha + ".json"), raw)


## Returns the raw bytes only if they still hash to `sha`.
func load_document(kind: String, sha: String) -> PackedByteArray:
	if not Schema.matches("sha256", sha) or (kind != "manifests" and kind != "descriptors"):
		return PackedByteArray()
	var raw: PackedByteArray = FileAccess.get_file_as_bytes(root.path_join(kind).path_join(sha + ".json"))
	return raw if Canonical.sha256_hex(raw) == sha else PackedByteArray()


func write_ref_index(asset_key: String, content: Dictionary) -> RefCounted:
	if not Schema.matches("sha256", asset_key):
		return Result.fail("invalid_request", "invalid asset_key")
	return _write_atomic(root.path_join("refs").path_join(asset_key + ".json"), JSON.stringify(content).to_utf8_buffer())


func read_ref_index(asset_key: String) -> Dictionary:
	if not Schema.matches("sha256", asset_key):
		return {}
	var path: String = root.path_join("refs").path_join(asset_key + ".json")
	if not FileAccess.file_exists(path):
		return {}
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(path))
	return parsed if parsed is Dictionary else {}


## Replaces the owner's pin set. Pinned blobs survive prune().
func pin(owner_id: String, shas: PackedStringArray) -> RefCounted:
	if owner_id.is_empty():
		return Result.fail("invalid_request", "owner_id required")
	var list: Array = []
	for sha: String in shas:
		if not Schema.matches("sha256", sha):
			return Result.fail("invalid_request", "invalid sha256 in pin set")
		if not list.has(sha):
			list.append(sha)
	_pins[owner_id] = list
	return _save_pins()


func unpin(owner_id: String) -> RefCounted:
	_pins.erase(owner_id)
	return _save_pins()


func is_pinned(sha: String) -> bool:
	for owner: String in _pins:
		if (_pins[owner] as Array).has(sha):
			return true
	return false


func pin_owners() -> PackedStringArray:
	var out := PackedStringArray()
	for k: String in _pins:
		out.append(k)
	out.sort()
	return out


## Removes unpinned blobs only. value = {"removed": Array[String], "bytes": int, "kept_pinned": int}.
func prune(dry_run: bool = true) -> RefCounted:
	var removed: Array = []
	var bytes: int = 0
	var kept: int = 0
	var blobs: String = root.path_join("blobs")
	if not DirAccess.dir_exists_absolute(blobs):
		return Result.success({"removed": removed, "bytes": 0, "kept_pinned": 0})
	for shard: String in DirAccess.get_directories_at(blobs):
		for sha: String in DirAccess.get_files_at(blobs.path_join(shard)):
			if not Schema.matches("sha256", sha):
				continue
			if is_pinned(sha):
				kept += 1
				continue
			var path: String = blobs.path_join(shard).path_join(sha)
			bytes += _file_size(path)
			removed.append(sha)
			if not dry_run:
				DirAccess.remove_absolute(path)
	return Result.success({"removed": removed, "bytes": bytes, "kept_pinned": kept})


func _file_size(path: String) -> int:
	var f: FileAccess = FileAccess.open(path, FileAccess.READ)
	return f.get_length() if f != null else -1


func _write_atomic(path: String, raw: PackedByteArray) -> RefCounted:
	DirAccess.make_dir_recursive_absolute(path.get_base_dir())
	var tmp: String = path + ".tmp"
	var f: FileAccess = FileAccess.open(tmp, FileAccess.WRITE)
	if f == null:
		return Result.fail("temporarily_unavailable", "cannot write cache file")
	f.store_buffer(raw)
	f.close()
	if DirAccess.rename_absolute(tmp, path) != OK:
		return Result.fail("temporarily_unavailable", "cannot replace cache file")
	return Result.success(path)


func _load_pins() -> void:
	var path: String = root.path_join("pins.json")
	if not FileAccess.file_exists(path):
		return
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(path))
	if parsed is Dictionary and (parsed as Dictionary).get("owners") is Dictionary:
		_pins = (parsed as Dictionary)["owners"]


func _save_pins() -> RefCounted:
	return _write_atomic(root.path_join("pins.json"), JSON.stringify({"owners": _pins}).to_utf8_buffer())
