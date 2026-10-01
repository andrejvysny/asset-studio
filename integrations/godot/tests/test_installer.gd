extends "res://tests/project_test_base.gd"
# Installer, offline verify and restore (offline resolver over a hand-filled blob cache; no network).

const Coordinator = preload("res://addons/assetstudio/project/as_mutation_coordinator.gd")
const Installer = preload("res://addons/assetstudio/project/as_installer.gd")
const Restore = preload("res://addons/assetstudio/project/as_restore.gd")
const Config = preload("res://addons/assetstudio/project/as_project_config.gd")
const Lock = preload("res://addons/assetstudio/project/as_project_lock.gd")
const BlobCache = preload("res://addons/assetstudio/core/as_blob_cache.gd")
const Resolver = preload("res://addons/assetstudio/core/as_asset_resolver.gd")
const Descriptor = preload("res://addons/assetstudio/core/as_asset_descriptor.gd")
const Manifest = preload("res://addons/assetstudio/core/as_delivery_manifest.gd")

const SERVER: String = "6f1c2a52-3c2e-4d4b-9a57-0b6f6f0c1d2e"
const VERSIONS: Dictionary = {
	"ver_00000000000000v1": ["descriptors/valid/primitive_prop.json", "manifests/valid/portable_primitive_prop.json",
			"glb/primitive_prop.portable.glb"],
	"ver_00000000000000v2": ["descriptors/valid/primitive_prop_v2.json", "manifests/valid/portable_primitive_prop_v2.json",
			"glb/primitive_prop_v2.portable.glb"],
}


## Fills a blob cache the way the resolver would after a download and returns {version_id: prep}.
func _fill_cache(cache: RefCounted) -> Dictionary:
	var preps: Dictionary = {}
	for ver: String in VERSIONS:
		var desc_raw: PackedByteArray = fixture(VERSIONS[ver][0])
		var man_raw: PackedByteArray = fixture(VERSIONS[ver][1])
		var glb: PackedByteArray = fixture(VERSIONS[ver][2])
		var desc: RefCounted = Descriptor.parse_bytes(desc_raw).value
		var man: RefCounted = Manifest.parse_bytes(man_raw).value
		cache.store_document("descriptors", desc.raw_sha256, desc_raw)
		cache.store_document("manifests", man.raw_sha256, man_raw)
		var f: Dictionary = man.data["files"][0]
		Fs.write_atomic(ProjectSettings.globalize_path(cache.staging_path(f["sha256"])), glb)
		var inst: RefCounted = cache.install_from_staging(cache.staging_path(f["sha256"]), f["sha256"], int(f["size"]))
		assert_true(inst.ok, "blob install: %s" % inst.describe())
		cache.write_ref_index(desc.asset_ref.key(), {"asset_key": desc.asset_ref.key(), "entries": {
				"portable_glb_v1": {"descriptor_sha256": desc.raw_sha256, "delivery_id": man.data["delivery_id"],
				"manifest_sha256": man.raw_sha256}}})
		preps[ver] = {"descriptor": desc, "manifest": man, "files": {"portable.glb": cache.blob_path(f["sha256"])},
				"delivery_id": man.data["delivery_id"], "ref": desc.asset_ref}
	return preps


func _install_all(root: String, preps: Dictionary, versions: Array) -> RefCounted:
	var c: RefCounted = Coordinator.new(root)
	assert_true(c.open("install").ok, "open")
	for ver: String in versions:
		var r: RefCounted = Installer.install(c, "assets/library", preps[ver]["ref"], preps[ver])
		assert_true(r.ok, "install %s: %s" % [ver, r.describe()])
	var done: RefCounted = c.commit()
	assert_true(done.ok, "commit: %s" % done.describe())
	return done


func _lock_for(preps: Dictionary, versions: Array) -> RefCounted:
	var lock: RefCounted = Lock.new_empty("0.1.0", "1.0.0")
	for ver: String in versions:
		var p: Dictionary = preps[ver]
		var m: RefCounted = p["manifest"]
		var delivery: Dictionary = {"delivery_id": p["delivery_id"], "manifest_sha256": m.raw_sha256,
				"profile_id": m.data["profile_id"], "profile_version": m.data["profile_version"]}
		assert_eq(lock.add_dependency(p["ref"], p["descriptor"].raw_sha256, "portable_glb_v1", delivery, []), "", "dep")
		var key: String = p["ref"].key()
		lock.add_binding("b-%s" % ver.right(2), key, "portable_glb_v1", {"mode": "preserve", "profile_id": null, "profile_sha256": null})
		lock.add_root("scene_binding", "b-%s" % ver.right(2), lock.closure(key))
	return lock


func _dir_of(root: String, p: Dictionary) -> String:
	return root.path_join("assets/library").path_join(p["ref"].key()).path_join(p["manifest"].raw_sha256)


func test_install_writes_bytes_receipt_and_import_seed() -> void:
	var cache: RefCounted = BlobCache.new(tmp_dir("cache"))
	var preps: Dictionary = _fill_cache(cache)
	var root: String = tmp_dir("proj")
	_install_all(root, preps, ["ver_00000000000000v1"])
	var p: Dictionary = preps["ver_00000000000000v1"]
	var dir: String = _dir_of(root, p)
	assert_eq(Fs.read_bytes(dir.path_join("portable.glb")), fixture(VERSIONS["ver_00000000000000v1"][2]), "bytes identical")
	var import_text: String = read_text(dir.path_join("portable.glb.import"))
	assert_eq(import_text, "[params]\n\narray_mesh/deduplicate_surfaces=false\nmeshes/generate_lods=true\nmaterials/extract=0\nnodes/apply_root_scale=true\n_subresources={}\n", "import seed")
	var receipt: RefCounted = CJson.parse_canonical(Fs.read_bytes(dir.path_join("receipt.json")))
	assert_true(receipt.ok, "receipt is canonical")
	assert_eq(receipt.value["manifest_sha256"], p["manifest"].raw_sha256, "receipt manifest sha")
	assert_eq(receipt.value["files"][0]["sha256"], p["manifest"].data["files"][0]["sha256"], "receipt file sha")
	assert_true(not DirAccess.dir_exists_absolute(root.path_join("assets/library/.staging")), "staging gone")
	var cfg: RefCounted = Config.defaults(SERVER)
	assert_true(Restore.verify_locked(root, _lock_for(preps, ["ver_00000000000000v1"]), cfg).ok, "verify ok")
	cleanup()


func test_blob_bytes_are_copied_not_linked() -> void:
	var cache: RefCounted = BlobCache.new(tmp_dir("cache"))
	var preps: Dictionary = _fill_cache(cache)
	var root: String = tmp_dir("proj")
	_install_all(root, preps, ["ver_00000000000000v1"])
	var p: Dictionary = preps["ver_00000000000000v1"]
	var f: FileAccess = FileAccess.open(_dir_of(root, p).path_join("portable.glb"), FileAccess.READ_WRITE)
	f.seek_end()
	f.store_8(0)
	f.close()
	assert_true(cache.verify_blob(p["manifest"].data["files"][0]["sha256"]), "editing the install never touches the cache")
	cleanup()


func test_two_versions_coexist_in_distinct_paths() -> void:
	var cache: RefCounted = BlobCache.new(tmp_dir("cache"))
	var preps: Dictionary = _fill_cache(cache)
	var root: String = tmp_dir("proj")
	var versions: Array = ["ver_00000000000000v1", "ver_00000000000000v2"]
	_install_all(root, preps, versions)
	var d1: String = _dir_of(root, preps[versions[0]])
	var d2: String = _dir_of(root, preps[versions[1]])
	assert_true(d1 != d2 and FileAccess.file_exists(d1.path_join("portable.glb")) and FileAccess.file_exists(d2.path_join("portable.glb")), "both installed")
	assert_true(Fs.sha256_file(d1.path_join("portable.glb")) != Fs.sha256_file(d2.path_join("portable.glb")), "different bytes")
	assert_true(Restore.verify_locked(root, _lock_for(preps, versions), Config.defaults(SERVER)).ok, "verify both")
	cleanup()


func test_tamper_is_detected_and_never_overwritten() -> void:
	var cache: RefCounted = BlobCache.new(tmp_dir("cache"))
	var preps: Dictionary = _fill_cache(cache)
	var root: String = tmp_dir("proj")
	var ver: String = "ver_00000000000000v1"
	_install_all(root, preps, [ver])
	var glb: String = _dir_of(root, preps[ver]).path_join("portable.glb")
	write_text(glb, "tampered")
	var lock: RefCounted = _lock_for(preps, [ver])
	var v: RefCounted = Restore.verify_locked(root, lock, Config.defaults(SERVER))
	assert_true(not v.ok and v.code == "integrity_mismatch", "verify reports the modified file")
	assert_true((v.details["problems"] as Array)[0].contains("portable.glb"), "names the file")
	var c: RefCounted = Coordinator.new(root)
	c.open("again")
	var r: RefCounted = Installer.install(c, "assets/library", preps[ver]["ref"], preps[ver])
	assert_true(not r.ok and r.code == "integrity_mismatch", "install refuses to touch a tampered delivery")
	c.close()
	assert_eq(read_text(glb), "tampered", "tampered file was not overwritten")
	cleanup()


func test_verify_detects_missing_install_missing_import_and_tampered_receipt() -> void:
	var cache: RefCounted = BlobCache.new(tmp_dir("cache"))
	var preps: Dictionary = _fill_cache(cache)
	var root: String = tmp_dir("proj")
	var ver: String = "ver_00000000000000v1"
	var lock: RefCounted = _lock_for(preps, [ver])
	var cfg: RefCounted = Config.defaults(SERVER)
	var empty: RefCounted = Restore.verify_locked(root, lock, cfg)
	assert_true(not empty.ok and (empty.details["problems"] as Array)[0].contains("not installed"), "nothing installed")
	_install_all(root, preps, [ver])
	var dir: String = _dir_of(root, preps[ver])
	DirAccess.remove_absolute(dir.path_join("portable.glb.import"))
	var no_import: RefCounted = Restore.verify_locked(root, lock, cfg)
	assert_true(not no_import.ok and (no_import.details["problems"] as Array)[0].contains("not imported"), "missing .import")
	write_text(dir.path_join("portable.glb.import"), "[params]\n")
	assert_true(Restore.verify_locked(root, lock, cfg).ok, "Godot-owned .import content is not interpreted")
	var receipt: Dictionary = JSON.parse_string(read_text(dir.path_join("receipt.json")))
	receipt["delivery_id"] = "dlv_00000000000000d9"
	Fs.write_atomic(dir.path_join("receipt.json"), CJson.encode(receipt).value)
	assert_true(not Restore.verify_locked(root, lock, cfg).ok, "receipt that disagrees with the lock")
	cleanup()


func _offline_resolver(cache: RefCounted) -> Node:
	var resolver: Node = Resolver.new()
	resolver.offline_only = true
	resolver.setup(null, cache)
	return resolver


func test_restore_from_cache_reproduces_identical_hashes() -> void:
	var cache: RefCounted = BlobCache.new(tmp_dir("cache"))
	var preps: Dictionary = _fill_cache(cache)
	var versions: Array = ["ver_00000000000000v1", "ver_00000000000000v2"]
	var first: String = tmp_dir("first")
	_install_all(first, preps, versions)
	var lock: RefCounted = _lock_for(preps, versions)
	var cfg: RefCounted = Config.defaults(SERVER)
	var fresh: String = tmp_dir("fresh")
	var resolver: Node = _offline_resolver(cache)
	var c: RefCounted = Coordinator.new(fresh)
	c.open("restore")
	var r: RefCounted = await Restore.restore(resolver, lock, cfg, c)
	assert_true(r.ok, "restore: %s" % (r.describe() if not r.ok else ""))
	assert_eq(r.value, {"installed": 2, "present": 0}, "installed both")
	assert_true(c.commit().ok, "commit")
	assert_eq(snapshot(fresh.path_join("assets"), []), snapshot(first.path_join("assets"), []), "identical hashes")
	# second run: everything present, nothing to do
	var c2: RefCounted = Coordinator.new(fresh)
	c2.open("restore")
	var again: RefCounted = await Restore.restore(resolver, lock, cfg, c2)
	assert_eq(again.value, {"installed": 0, "present": 2}, "idempotent")
	c2.close()
	resolver.free()
	cleanup()


func test_restore_refuses_a_different_delivery_than_the_locked_one() -> void:
	var cache: RefCounted = BlobCache.new(tmp_dir("cache"))
	var preps: Dictionary = _fill_cache(cache)
	var ver: String = "ver_00000000000000v1"
	var cfg: RefCounted = Config.defaults(SERVER)
	var resolver: Node = _offline_resolver(cache)
	# 1) manifest sha differs from the lock: integrity_mismatch
	var lock: RefCounted = _lock_for(preps, [ver])
	var key: String = preps[ver]["ref"].key()
	lock.dependencies()[key]["deliveries"]["portable_glb_v1"]["manifest_sha256"] = "ab".repeat(32)
	var c: RefCounted = Coordinator.new(tmp_dir("p1"))
	c.open("restore")
	var r: RefCounted = await Restore.restore(resolver, lock, cfg, c)
	assert_true(not r.ok and r.code == "integrity_mismatch", "manifest pin: %s" % r.describe())
	c.close()
	# 2) delivery id differs: the cache never substitutes another delivery
	lock = _lock_for(preps, [ver])
	lock.dependencies()[key]["deliveries"]["portable_glb_v1"]["delivery_id"] = "dlv_00000000000000d9"
	c = Coordinator.new(tmp_dir("p2"))
	c.open("restore")
	r = await Restore.restore(resolver, lock, cfg, c)
	assert_true(not r.ok, "delivery pin must fail offline")
	c.close()
	# 3) descriptor sha differs
	lock = _lock_for(preps, [ver])
	lock.dependencies()[key]["descriptor_sha256"] = "cd".repeat(32)
	c = Coordinator.new(tmp_dir("p3"))
	c.open("restore")
	r = await Restore.restore(resolver, lock, cfg, c)
	assert_true(not r.ok and r.code == "integrity_mismatch", "descriptor pin")
	c.close()
	resolver.free()
	cleanup()


func test_restore_offline_with_empty_cache_fails_without_network() -> void:
	var preps: Dictionary = _fill_cache(BlobCache.new(tmp_dir("seed")))
	var resolver: Node = _offline_resolver(BlobCache.new(tmp_dir("empty")))
	var c: RefCounted = Coordinator.new(tmp_dir("p"))
	c.open("restore")
	var r: RefCounted = await Restore.restore(resolver, _lock_for(preps, ["ver_00000000000000v1"]), Config.defaults(SERVER), c)
	assert_true(not r.ok and r.code == "temporarily_unavailable", "uncached delivery cannot be restored offline")
	c.close()
	resolver.free()
	cleanup()
