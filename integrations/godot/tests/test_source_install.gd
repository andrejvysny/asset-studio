extends "res://tests/source_test_base.gd"
# godot_static_source_v1 install: preserved archive, derived relocated tree, receipt, trust gate, dependency
# resolution, crash safety, verify and restore.

const Restore = preload("res://addons/assetstudio/project/as_restore.gd")
const Config = preload("res://addons/assetstudio/project/as_project_config.gd")
const AddCommand = preload("res://addons/assetstudio/project/as_add_command.gd")
const Resolver = preload("res://addons/assetstudio/core/as_asset_resolver.gd")
const SourceInstall = preload("res://addons/assetstudio/project/as_srcpkg_install.gd")

const SERVER: String = "6f1c2a52-3c2e-4d4b-9a57-0b6f6f0c1d2e"
const DEP_KEY: String = "3e4cecabdb6c9bac29d5f9c655852e10f1d96abdb437cee386bfecf3974b1cbf"
const VALID: Array = [["csg_hut", "csg_hut"], ["custom_shader_crystal", "custom_shader_crystal"],
		["primitive_prop", "primitive_prop"], ["primitive_prop_v2", "primitive_prop_v2"],
		["textured_tree", "textured_tree"], ["vertex_color_rock_with_collision", "vertex_color_rock_with_collision"],
		["array_mesh_prop", "array_mesh_prop"]]


func _prep(name: String) -> Dictionary:
	return source_prep(zip_file("valid", name), name)


func _trusted() -> Dictionary:
	return {"trust_shaders": true}


func _zip_text(zip_abs: String, member: String) -> String:
	var z := ZIPReader.new()
	z.open(zip_abs)
	var text: String = z.read_file(member).get_string_from_utf8()
	z.close()
	return text


func test_each_valid_fixture_installs_archive_tree_and_receipt() -> void:
	for pair: Array in VALID:
		var root: String = tmp_dir("proj")
		var prep: Dictionary = _prep(pair[0])
		var done: RefCounted = install_all(root, [{"prep": prep, "opts": _trusted()}])
		assert_true(done.ok, "%s: %s" % [pair[0], done.describe()])
		var dir: String = delivery_dir(root, prep)
		assert_eq(Fs.read_bytes(dir.path_join("source.zip")), Fs.read_bytes(zip_file("valid", pair[0])), "%s: archive preserved byte-identical" % pair[0])
		var receipt: RefCounted = CJson.parse_canonical(Fs.read_bytes(dir.path_join("receipt.json")))
		assert_true(receipt.ok, "%s: canonical receipt" % pair[0])
		var rc: Dictionary = receipt.value
		assert_eq(rc["representation"], SOURCE_REP, "representation")
		assert_eq(rc["files"][0]["path"], "source.zip", "receipt lists the archive")
		assert_eq(rc["files"][0]["sha256"], prep["manifest"].data["files"][0]["sha256"], "archive hash is the manifest's")
		var src: Dictionary = rc["source"]
		assert_eq(src["original_files"].size(), src["installed_files"].size(), "%s: every member has an installed entry" % pair[0])
		for f: Dictionary in src["installed_files"]:
			assert_eq(Fs.sha256_file(dir.path_join(f["path"])), f["sha256"], "%s: %s hash recorded" % [pair[0], f["path"]])
		var expect: Dictionary = {"asset_key": prep["ref"].key(), "manifest_sha256": prep["manifest"].raw_sha256,
				"delivery_id": prep["delivery_id"], "representation": SOURCE_REP}
		assert_eq(Array(Installer.check_install(dir, expect)), [], "%s: check_install is clean" % pair[0])
		assert_true(not DirAccess.dir_exists_absolute(root.path_join("assets/library/.staging")), "staging removed")
	cleanup()


func test_rewrites_only_references_and_drops_foreign_uids() -> void:
	var root: String = tmp_dir("proj")
	var prep: Dictionary = _prep("primitive_prop")
	assert_true(install_all(root, [{"prep": prep}]).ok, "install")
	var dir: String = delivery_dir(root, prep)
	var base: String = "res://assets/library/%s/%s/source" % [prep["ref"].key(), prep["manifest"].raw_sha256]
	var original: String = _zip_text(zip_file("valid", "primitive_prop"), "scenes/prop.tscn")
	var expected: String = original.replace(" uid=\"uid://c8w2q1prop001\"", "").replace(
			"uid=\"uid://b3k1m0pa1n7ro\" path=\"res://materials/prop_mat.tres\"", "path=\"%s/materials/prop_mat.tres\"" % base)
	assert_eq(read_text(dir.path_join("source/scenes/prop.tscn")), expected, "scene: uid removed, path remapped, all else identical")
	assert_eq(read_text(dir.path_join("source/materials/prop_mat.tres")),
			_zip_text(zip_file("valid", "primitive_prop"), "materials/prop_mat.tres").replace(" uid=\"uid://b3k1m0pa1n7ro\"", ""),
			"material: only the header uid is gone")
	var src: Dictionary = read_json(dir.path_join("receipt.json"))["source"]
	for f: Dictionary in src["installed_files"]:
		assert_true(f["rewritten"], "%s was rewritten" % f["path"])
		assert_true(f["sha256"] != f["original_sha256"], "separate hash for the rewritten file")
	cleanup()


func test_binary_members_are_copied_byte_identical() -> void:
	var root: String = tmp_dir("proj")
	var rock: Dictionary = _prep("vertex_color_rock_with_collision")
	var tree: Dictionary = _prep("textured_tree")
	assert_true(install_all(root, [{"prep": rock}, {"prep": tree}]).ok, "install")
	var z := ZIPReader.new()
	z.open(zip_file("valid", "vertex_color_rock_with_collision"))
	assert_eq(Fs.read_bytes(delivery_dir(root, rock).path_join("source/models/rock.glb")), z.read_file("models/rock.glb"), "glb")
	z.close()
	z.open(zip_file("valid", "textured_tree"))
	assert_eq(Fs.read_bytes(delivery_dir(root, tree).path_join("source/textures/bark.png")), z.read_file("textures/bark.png"), "png")
	z.close()
	var rec: Dictionary = read_json(delivery_dir(root, rock).path_join("receipt.json"))["source"]
	var glb_entry: Dictionary = (rec["installed_files"] as Array).filter(func(f: Dictionary) -> bool: return f["path"].ends_with(".glb"))[0]
	assert_true(not glb_entry["rewritten"] and glb_entry["sha256"] == glb_entry["original_sha256"], "binary is not marked rewritten")
	cleanup()


func test_hostile_fixtures_are_refused_and_nothing_is_written() -> void:
	var index: Variant = JSON.parse_string(read_text(contracts_dir().path_join("fixtures/INDEX.json")))
	var seen: int = 0
	for e: Dictionary in index["fixtures"]:
		if not str(e["path"]).begins_with("source_packages/hostile/"):
			continue
		seen += 1
		var root: String = tmp_dir("hostile")
		var abs_zip: String = contracts_dir().path_join("fixtures").path_join(e["path"])
		var prep: Dictionary = source_prep(abs_zip, "primitive_prop")
		var r: RefCounted = install_all(root, [{"prep": prep, "opts": _trusted()}])
		assert_true(not r.ok, "%s must be refused" % e["path"])
		if not r.ok:
			assert_eq(r.code, e["expected"], "code of %s" % e["path"])
			assert_eq(r.details.get("detail"), e["detail"], "detail of %s" % e["path"])
		assert_eq(snapshot(root, [".assetstudio"]), {}, "%s: nothing is written to the project" % e["path"])
		assert_true(not DirAccess.dir_exists_absolute(root.path_join("assets/library")), "%s: no managed dir" % e["path"])
	assert_true(seen >= 18, "all hostile fixtures were exercised (%d)" % seen)
	cleanup()


func test_shader_packages_need_explicit_trust() -> void:
	var root: String = tmp_dir("proj")
	var prep: Dictionary = _prep("custom_shader_crystal")
	var r: RefCounted = install_all(root, [{"prep": prep}])
	assert_true(not r.ok and r.code == "unsafe_package", "refused without trust: %s" % r.describe())
	assert_true(r.message.contains("shader trust required"), "reason: %s" % r.message)
	assert_eq(snapshot(root, [".assetstudio"]), {}, "nothing written")
	var denied: RefCounted = install_all(root, [{"prep": prep, "opts": {"trust_shaders": false}}])
	assert_true(not denied.ok, "explicit false is refused too")
	var ok: RefCounted = install_all(root, [{"prep": prep, "opts": _trusted()}])
	assert_true(ok.ok, "installs with trust: %s" % ok.describe())
	var dir: String = delivery_dir(root, prep)
	var base: String = "res://assets/library/%s/%s/source" % [prep["ref"].key(), prep["manifest"].raw_sha256]
	assert_true(read_text(dir.path_join("source/shaders/crystal.gdshader")).contains("#include \"%s/shaders/common.gdshaderinc\"" % base), "shader include remapped")
	assert_eq(read_json(dir.path_join("receipt.json"))["source"]["shader_trust"], true, "trust recorded in the receipt")
	cleanup()


func test_prop_cluster_resolves_its_dependency_to_the_installed_portable_entrypoint() -> void:
	var root: String = tmp_dir("proj")
	var dep: Dictionary = portable_prep()
	assert_eq(dep["ref"].key(), DEP_KEY, "fixture dependency key")
	var cluster: Dictionary = source_prep(zip_file("valid", "prop_cluster"), "prop_cluster", "source_prop_cluster")
	var keys: Array = [cluster["ref"].key(), DEP_KEY]
	var done: RefCounted = install_all(root, [{"prep": dep}, {"prep": cluster, "opts": {"closure_keys": keys}}])
	assert_true(done.ok, "install: %s" % done.describe())
	var scene: String = read_text(delivery_dir(root, cluster).path_join("source/scenes/cluster.tscn"))
	var portable_res: String = "res://assets/library/%s/%s/portable.glb" % [DEP_KEY, dep["manifest"].raw_sha256]
	assert_true(scene.contains("path=\"%s\"" % portable_res), "ext_resource points at the portable delivery")
	assert_true(FileAccess.file_exists(delivery_dir(root, dep).path_join("portable.glb")), "the entrypoint exists on disk")
	assert_true(not scene.contains("uid://"), "no foreign uid left")
	assert_true(not scene.contains("res://deps/"), "no original reference left")
	cleanup()


func test_dependency_outside_the_closure_is_refused() -> void:
	var root: String = tmp_dir("proj")
	var cluster: Dictionary = source_prep(zip_file("valid", "prop_cluster"), "prop_cluster", "source_prop_cluster")
	var r: RefCounted = install_all(root, [{"prep": cluster}])
	assert_true(not r.ok and r.code == "unsupported_source_dependency", "no closure given: %s" % r.describe())
	r = install_all(root, [{"prep": cluster, "opts": {"closure_keys": [cluster["ref"].key()]}}])
	assert_true(not r.ok and r.code == "unsupported_source_dependency", "dependency not in the closure: %s" % r.describe())
	assert_eq(snapshot(root, [".assetstudio"]), {}, "nothing written")
	# the delivery manifest does not pin the dependency the package maps
	var unpinned: Dictionary = source_prep(zip_file("valid", "prop_cluster"), "prop_cluster")
	r = install_all(root, [{"prep": unpinned, "opts": {"closure_keys": [DEP_KEY]}}])
	assert_true(not r.ok and r.details.get("detail") == "dependency_not_in_closure", "unpinned dependency: %s" % r.describe())
	cleanup()


func test_two_versions_with_shared_uids_install_side_by_side() -> void:
	var root: String = tmp_dir("proj")
	var v1: Dictionary = _prep("primitive_prop")
	var v2: Dictionary = _prep("primitive_prop_v2")
	assert_true(install_all(root, [{"prep": v1}, {"prep": v2}]).ok, "both install")
	assert_true(delivery_dir(root, v1) != delivery_dir(root, v2), "distinct managed directories")
	for prep: Dictionary in [v1, v2]:
		var tree_files: Array = snapshot(delivery_dir(root, prep).path_join("source")).keys()
		for f: String in tree_files:
			var text: String = read_text(delivery_dir(root, prep).path_join("source").path_join(f))
			assert_true(not text.contains("uid://"), "%s carries no uid" % f)
	cleanup()


## Installs `second` into `root` in one transaction that crashes after `crash_at` atomic steps (-1 = none).
## Returns {"result", "steps"}.
func _install_with_crash(root: String, second: Dictionary, crash_at: int) -> Dictionary:
	var c: RefCounted = Coordinator.new(root)
	assert_true(c.open("install").ok, "open")
	assert_true(Installer.install(c, MANAGED, second["ref"], second, {}).ok, "staged")
	Coordinator.fail_after_step = crash_at
	var r: RefCounted = c.commit()
	Coordinator.fail_after_step = -1
	return {"result": r, "steps": c.steps_taken()}


func test_crash_between_stage_and_move_leaves_the_previous_install_intact() -> void:
	var first: Dictionary = _prep("primitive_prop")
	var second: Dictionary = _prep("textured_tree")
	var ref_root: String = tmp_dir("ref")
	assert_true(install_all(ref_root, [{"prep": first}]).ok, "first install")
	var total: int = _install_with_crash(ref_root, second, -1)["steps"]
	assert_true(total >= 4, "several crash points (%d)" % total)
	var expect_second: Dictionary = {"asset_key": second["ref"].key(), "manifest_sha256": second["manifest"].raw_sha256,
			"delivery_id": second["delivery_id"], "representation": SOURCE_REP}
	var saw_old: bool = false
	var saw_new: bool = false
	for n: int in range(1, total + 1):
		var root: String = tmp_dir("crash")
		assert_true(install_all(root, [{"prep": first}]).ok, "first install")
		var before: Dictionary = snapshot(delivery_dir(root, first))
		var run: Dictionary = _install_with_crash(root, second, n)
		assert_eq(run["result"].code, "simulated_crash", "step %d crashes" % n)
		var rec: RefCounted = Coordinator.recover_project(root)
		assert_true(rec.ok, "recover after step %d: %s" % [n, rec.describe()])
		assert_eq(snapshot(delivery_dir(root, first)), before, "previous install untouched after crash at step %d" % n)
		var landed: bool = DirAccess.dir_exists_absolute(delivery_dir(root, second))
		saw_new = saw_new or landed
		saw_old = saw_old or not landed
		if landed:
			assert_eq(Array(Installer.check_install(delivery_dir(root, second), expect_second)), [], "a delivery that landed is complete (step %d)" % n)
		assert_true(not DirAccess.dir_exists_absolute(root.path_join("assets/library/.staging")), "no staging left (step %d)" % n)
	assert_true(saw_old and saw_new, "crash points on both sides of the commit marker")
	cleanup()


func test_failed_validation_leaves_a_previous_install_untouched() -> void:
	var root: String = tmp_dir("proj")
	var prep: Dictionary = _prep("primitive_prop")
	assert_true(install_all(root, [{"prep": prep}]).ok, "install")
	var before: Dictionary = snapshot(root, [".assetstudio"])
	var bad: Dictionary = source_prep(contracts_dir().path_join("fixtures/source_packages/hostile/connection_in_scene.zip"), "primitive_prop")
	assert_true(not install_all(root, [{"prep": bad}]).ok, "hostile package refused")
	assert_eq(snapshot(root, [".assetstudio"]), before, "project unchanged")
	cleanup()


func test_check_install_detects_a_modified_rewritten_file_and_archive() -> void:
	var root: String = tmp_dir("proj")
	var prep: Dictionary = _prep("primitive_prop")
	assert_true(install_all(root, [{"prep": prep}]).ok, "install")
	var dir: String = delivery_dir(root, prep)
	var expect: Dictionary = {"asset_key": prep["ref"].key(), "manifest_sha256": prep["manifest"].raw_sha256,
			"delivery_id": prep["delivery_id"], "representation": SOURCE_REP}
	assert_eq(Array(Installer.check_install(dir, expect)), [], "clean")
	var scene: String = dir.path_join("source/scenes/prop.tscn")
	var good: String = read_text(scene)
	write_text(scene, good + "\n; edited\n")
	var problems: PackedStringArray = Installer.check_install(dir, expect)
	assert_true(problems.size() == 1 and problems[0].contains("source/scenes/prop.tscn"), "modified rewritten file named: %s" % str(problems))
	var r: RefCounted = install_all(root, [{"prep": prep}])
	assert_true(not r.ok and r.code == "integrity_mismatch", "re-install refuses to overwrite it")
	assert_eq(read_text(scene), good + "\n; edited\n", "the edit was not overwritten")
	write_text(scene, good)
	assert_eq(Array(Installer.check_install(dir, expect)), [], "clean again")
	write_text(dir.path_join("source.zip"), "tampered")
	assert_true(Installer.check_install(dir, expect)[0].contains("source.zip"), "modified archive is detected too")
	DirAccess.remove_absolute(scene)
	assert_true(Installer.check_install(dir, expect).size() >= 1, "missing derived file")
	cleanup()


func _source_lock(preps: Array, deps: Array) -> RefCounted:
	var lock: RefCounted = Lock.new_empty("0.1.0", "1.0.0")
	for d: Dictionary in deps:
		lock_dependency(lock, d, [])
	for p: Dictionary in preps:
		lock_dependency(lock, p, deps.map(func(d: Dictionary) -> String: return d["ref"].key()))
		var key: String = p["ref"].key()
		lock.add_binding("b-%s" % key.left(6), key, SOURCE_REP, {"mode": "preserve", "profile_id": null, "profile_sha256": null})
		lock.add_root("scene_binding", "b-%s" % key.left(6), lock.closure(key))
	return lock


func test_verify_locked_covers_source_deliveries() -> void:
	var root: String = tmp_dir("proj")
	var prep: Dictionary = _prep("textured_tree")
	assert_true(install_all(root, [{"prep": prep}]).ok, "install")
	var cfg: RefCounted = Config.defaults(SERVER)
	var lock: RefCounted = _source_lock([prep], [])
	assert_true(Restore.verify_locked(root, lock, cfg).ok, "verify ok")
	write_text(delivery_dir(root, prep).path_join("source/materials/bark.tres"), "tampered")
	var v: RefCounted = Restore.verify_locked(root, lock, cfg)
	assert_true(not v.ok and (v.details["problems"] as Array)[0].contains("bark.tres"), "verify names the modified derived file")
	cleanup()


func _offline_resolver(cache: RefCounted) -> Node:
	var resolver: Node = Resolver.new()
	resolver.offline_only = true
	resolver.setup(null, cache)
	return resolver


func test_restore_reinstalls_source_deliveries_with_identical_hashes() -> void:
	var cache: RefCounted = BlobCache.new(tmp_dir("cache"))
	var dep: Dictionary = portable_prep()
	var cluster: Dictionary = source_prep(zip_file("valid", "prop_cluster"), "prop_cluster", "source_prop_cluster")
	var tree: Dictionary = _prep("textured_tree")
	cache_delivery(cache, dep, PORTABLE_REP)
	cache_delivery(cache, cluster, SOURCE_REP)
	cache_delivery(cache, tree, SOURCE_REP)
	var keys: Array = [cluster["ref"].key(), DEP_KEY]
	var first: String = tmp_dir("first")
	assert_true(install_all(first, [{"prep": dep}, {"prep": cluster, "opts": {"closure_keys": keys}}, {"prep": tree}]).ok, "first install")
	var lock: RefCounted = _source_lock([cluster, tree], [dep])
	var fresh: String = tmp_dir("fresh")
	var resolver: Node = _offline_resolver(cache)
	var c: RefCounted = Coordinator.new(fresh)
	c.open("restore")
	var r: RefCounted = await Restore.restore(resolver, lock, Config.defaults(SERVER), c)
	assert_true(r.ok, "restore: %s" % (r.describe() if not r.ok else ""))
	assert_true(c.commit().ok, "commit")
	assert_eq(snapshot(fresh.path_join("assets")), snapshot(first.path_join("assets")), "identical tree after restore")
	assert_true(Restore.verify_locked(fresh, lock, Config.defaults(SERVER)).ok, "restored tree verifies")
	resolver.free()
	cleanup()


func test_restore_requires_shader_trust() -> void:
	var cache: RefCounted = BlobCache.new(tmp_dir("cache"))
	var crystal: Dictionary = _prep("custom_shader_crystal")
	cache_delivery(cache, crystal, SOURCE_REP)
	var lock: RefCounted = _source_lock([crystal], [])
	var resolver: Node = _offline_resolver(cache)
	var root: String = tmp_dir("fresh")
	var c: RefCounted = Coordinator.new(root)
	c.open("restore")
	var denied: RefCounted = await Restore.restore(resolver, lock, Config.defaults(SERVER), c)
	assert_true(not denied.ok and denied.code == "unsafe_package", "refused without trust: %s" % denied.describe())
	c.close()
	c = Coordinator.new(root)
	c.open("restore")
	var ok: RefCounted = await Restore.restore(resolver, lock, Config.defaults(SERVER), c, {"trust_shaders": true})
	assert_true(ok.ok, "restores with trust")
	c.close()
	resolver.free()
	cleanup()


func test_add_binding_wires_a_source_delivery() -> void:
	var root: String = tmp_dir("proj")
	var prep: Dictionary = _prep("csg_hut")
	var c: RefCounted = Coordinator.new(root)
	assert_true(c.open("add").ok, "open")
	var lock: RefCounted = Lock.new_empty("0.1.0", "1.0.0")
	var config: RefCounted = Config.defaults(SERVER)
	var nodes: Array = [{"ref": prep["ref"], "prep": prep, "requires": []}]
	var r: RefCounted = AddCommand.apply_binding(c, config, lock, nodes, "hut", {"mode": "preserve", "profile_id": null, "profile_sha256": null}, SOURCE_REP)
	assert_true(r.ok, "apply_binding: %s" % r.describe())
	assert_true(c.commit().ok, "commit")
	assert_eq(lock.bindings()["hut"]["representation"], SOURCE_REP, "binding representation")
	var wrapper: String = read_text(root.path_join("assets/prefabs/hut.tscn"))
	var entry: String = "res://assets/library/%s/%s/source/scenes/hut.tscn" % [prep["ref"].key(), prep["manifest"].raw_sha256]
	assert_true(wrapper.contains("path=\"%s\"" % entry), "wrapper instances the derived entry scene")
	assert_true(FileAccess.file_exists(entry.replace("res://", root + "/")), "entry scene exists")
	var state_file: String = root.path_join(".assetstudio/state.json")
	assert_true(not FileAccess.file_exists(state_file) or (read_json(state_file)["pending_import"] as Array).is_empty(),
			"no pending import for a source binding")
	c.close()
	cleanup()


func test_add_command_option_checks() -> void:
	assert_eq(AddCommand._representation_error({"representation": "mobile_glb_v1"}) != "", true, "unsupported representation")
	assert_eq(AddCommand._representation_error({"representation": SOURCE_REP, "profile": "p"}) != "", true, "profile with source")
	assert_eq(AddCommand._representation_error({"trust-shaders": true}) != "", true, "trust flag needs the source representation")
	assert_eq(AddCommand._representation_error({"representation": SOURCE_REP, "trust-shaders": true}), "", "valid")
	assert_eq(AddCommand._representation_error({}), "", "default portable")
