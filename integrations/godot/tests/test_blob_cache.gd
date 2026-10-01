extends "res://tests/test_case.gd"

const BlobCache = preload("res://addons/assetstudio/core/as_blob_cache.gd")
const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")

var _roots: Array[String] = []


func _new_cache() -> RefCounted:
	var root: String = "user://as_test_cache_%d_%d" % [Time.get_ticks_usec(), randi()]
	_roots.append(root)
	return BlobCache.new(root)


func _stage(cache: RefCounted, name: String, data: PackedByteArray) -> String:
	var path: String = cache.root.path_join("staging").path_join(name)
	DirAccess.make_dir_recursive_absolute(path.get_base_dir())
	var f: FileAccess = FileAccess.open(path, FileAccess.WRITE)
	f.store_buffer(data)
	f.close()
	return path


func _cleanup() -> void:
	for root: String in _roots:
		_remove_tree(root)


func _remove_tree(path: String) -> void:
	if not DirAccess.dir_exists_absolute(path):
		return
	for d: String in DirAccess.get_directories_at(path):
		_remove_tree(path.path_join(d))
	for f: String in DirAccess.get_files_at(path):
		DirAccess.remove_absolute(path.path_join(f))
	DirAccess.remove_absolute(path)


func test_install_verified_blob() -> void:
	var cache: RefCounted = _new_cache()
	var data: PackedByteArray = "hello blob".to_utf8_buffer()
	var sha: String = Canonical.sha256_hex(data)
	var r: RefCounted = cache.install_from_staging(_stage(cache, "a.part", data), sha, data.size())
	assert_true(r.ok, "install: %s" % r.describe())
	assert_true(cache.has_blob(sha), "has_blob")
	assert_true(cache.verify_blob(sha), "verify_blob")
	assert_eq(FileAccess.get_file_as_bytes(cache.blob_path(sha)), data, "bytes")
	assert_true(cache.blob_path(sha).contains("/blobs/%s/%s" % [sha.substr(0, 2), sha]), "layout")
	_cleanup()


func test_install_rejects_mismatch_and_deletes_staging() -> void:
	var cache: RefCounted = _new_cache()
	var data: PackedByteArray = "payload".to_utf8_buffer()
	var staged: String = _stage(cache, "b.part", data)
	var wrong: String = Canonical.sha256_hex("other".to_utf8_buffer())
	var r: RefCounted = cache.install_from_staging(staged, wrong, data.size())
	assert_eq(r.code, "integrity_mismatch", "code")
	assert_true(not cache.has_blob(wrong), "nothing installed")
	assert_true(not FileAccess.file_exists(staged), "staging removed")
	var staged2: String = _stage(cache, "c.part", data)
	assert_eq(cache.install_from_staging(staged2, Canonical.sha256_hex(data), data.size() + 1).code, "integrity_mismatch", "size")
	_cleanup()


func test_existing_blob_is_not_overwritten() -> void:
	var cache: RefCounted = _new_cache()
	var data: PackedByteArray = "same".to_utf8_buffer()
	var sha: String = Canonical.sha256_hex(data)
	assert_true(cache.install_from_staging(_stage(cache, "1.part", data), sha, data.size()).ok, "first")
	var before: int = FileAccess.get_modified_time(cache.blob_path(sha))
	assert_true(cache.install_from_staging(_stage(cache, "2.part", data), sha, data.size()).ok, "second")
	assert_eq(FileAccess.get_modified_time(cache.blob_path(sha)), before, "untouched")
	assert_true(not FileAccess.file_exists(cache.root.path_join("staging/2.part")), "dup staging removed")
	_cleanup()


func test_pin_unpin_prune() -> void:
	var cache: RefCounted = _new_cache()
	var shas: PackedStringArray = PackedStringArray()
	for n: String in ["one", "two", "three"]:
		var data: PackedByteArray = n.to_utf8_buffer()
		var sha: String = Canonical.sha256_hex(data)
		cache.install_from_staging(_stage(cache, n + ".part", data), sha, data.size())
		shas.append(sha)
	assert_true(cache.pin("world-1", PackedStringArray([shas[0]])).ok, "pin")
	assert_true(cache.pin("preview", PackedStringArray([shas[0], shas[1]])).ok, "pin 2")
	var dry: RefCounted = cache.prune(true)
	assert_eq(dry.value["removed"], [shas[2]], "dry run lists only unpinned")
	assert_true(cache.has_blob(shas[2]), "dry run removes nothing")
	assert_true(cache.prune(false).ok, "prune")
	assert_true(cache.has_blob(shas[0]) and cache.has_blob(shas[1]) and not cache.has_blob(shas[2]), "only unpinned removed")
	# Pins persist across instances.
	var reopened: RefCounted = BlobCache.new(cache.root)
	assert_true(reopened.is_pinned(shas[1]), "pins persisted")
	reopened.unpin("preview")
	assert_true(not reopened.is_pinned(shas[1]) and reopened.is_pinned(shas[0]), "unpin only that owner")
	reopened.prune(false)
	assert_true(not reopened.has_blob(shas[1]) and reopened.has_blob(shas[0]), "pinned survives, unpinned goes")
	_cleanup()


func test_documents_are_verified_on_read() -> void:
	var cache: RefCounted = _new_cache()
	var raw: PackedByteArray = "{\"a\":1}".to_utf8_buffer()
	var sha: String = Canonical.sha256_hex(raw)
	assert_true(cache.store_document("manifests", sha, raw).ok, "store")
	assert_eq(cache.load_document("manifests", sha), raw, "load")
	assert_eq(cache.store_document("manifests", sha, "x".to_utf8_buffer()).code, "integrity_mismatch", "reject wrong bytes")
	var f: FileAccess = FileAccess.open(cache.root.path_join("manifests").path_join(sha + ".json"), FileAccess.WRITE)
	f.store_string("tampered")
	f.close()
	assert_true(cache.load_document("manifests", sha).is_empty(), "tampered doc rejected")
	_cleanup()


func test_invalid_ids_rejected() -> void:
	var cache: RefCounted = _new_cache()
	assert_true(not cache.has_blob("../../etc/passwd"), "traversal sha")
	assert_true(not cache.pin("o", PackedStringArray(["xyz"])).ok, "bad pin sha")
	assert_true(not cache.write_ref_index("../x", {}).ok, "bad asset key")
	_cleanup()
