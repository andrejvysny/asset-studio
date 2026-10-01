extends "res://tests/project_test_base.gd"
# Project config parsing and lock helpers (closure maintenance, binding ids).

const Config = preload("res://addons/assetstudio/project/as_project_config.gd")
const Lock = preload("res://addons/assetstudio/project/as_project_lock.gd")

const SERVER: String = "6f1c2a52-3c2e-4d4b-9a57-0b6f6f0c1d2e"


func _valid_config() -> Dictionary:
	return {"schema_version": 1, "server_id": SERVER,
			"libraries": [{"library_id": "prj_0000000000000001", "label": "fantasy-base"}],
			"managed_root": "res://assets/library", "prefab_root": "res://assets/prefabs",
			"material_profiles_dir": "res://integration/material_profiles",
			"default_material_policy": {"mode": "preserve", "profile_id": null}}


func test_config_round_trip_is_canonical() -> void:
	var raw: PackedByteArray = CJson.encode(_valid_config()).value
	var r: RefCounted = Config.parse_bytes(raw)
	assert_true(r.ok, r.describe())
	assert_eq(r.value.to_bytes().value, raw, "canonical bytes")
	assert_eq(r.value.managed_rel(), "assets/library", "managed_rel")


func test_config_rejects_unknown_keys_and_bad_values() -> void:
	var cases: Array[Callable] = [
		func(d: Dictionary) -> void: d["extra"] = 1,
		func(d: Dictionary) -> void: d.erase("prefab_root"),
		func(d: Dictionary) -> void: d["managed_root"] = "assets/library",
		func(d: Dictionary) -> void: d["managed_root"] = "res://assets/../library",
		func(d: Dictionary) -> void: d["prefab_root"] = "res://",
		func(d: Dictionary) -> void: d["material_profiles_dir"] = "res://a\\b",
		func(d: Dictionary) -> void: d["server_id"] = "not-a-uuid",
		func(d: Dictionary) -> void: d["schema_version"] = 2,
		func(d: Dictionary) -> void: d["libraries"] = [{"library_id": "prj_x", "label": "a"}],
		func(d: Dictionary) -> void: d["libraries"][0]["extra"] = true,
		func(d: Dictionary) -> void: d["default_material_policy"] = {"mode": "override", "profile_id": null},
		func(d: Dictionary) -> void: d["default_material_policy"] = {"mode": "project_mapping", "profile_id": null},
		func(d: Dictionary) -> void: d["default_material_policy"] = {"mode": "preserve", "profile_id": "x"},
	]
	for i: int in cases.size():
		var d: Dictionary = _valid_config()
		cases[i].call(d)
		assert_true(not Config.parse_bytes(CJson.encode(d).value).ok, "case %d must be rejected" % i)
	assert_true(not Config.parse_bytes("[]".to_utf8_buffer()).ok, "array")
	assert_true(not Config.parse_bytes(PackedByteArray()).ok, "empty")


func test_config_accepts_hand_formatted_json() -> void:
	var pretty: PackedByteArray = JSON.stringify(_valid_config(), "\t").to_utf8_buffer()
	assert_true(Config.parse_bytes(pretty).ok, "pretty-printed config is fine; only the writer is canonical")


func test_config_add_library_is_idempotent() -> void:
	var c: RefCounted = Config.defaults(SERVER)
	c.add_library("prj_0000000000000001", "a")
	c.add_library("prj_0000000000000001", "b")
	assert_eq(c.libraries.size(), 1, "one library")
	assert_true(Config.parse_bytes(c.to_bytes().value).ok, "defaults + library parse")


func _two_versions() -> RefCounted:
	return Lock.parse_bytes(fixture("locks/valid/two_versions.json")).value


func test_closure_and_remove_binding_maintain_dependencies() -> void:
	var lock: RefCounted = _two_versions()
	var a: String = "3e4cecabdb6c9bac29d5f9c655852e10f1d96abdb437cee386bfecf3974b1cbf"
	var b: String = "19c1fc59f561838a6064f3903b12ec283f1b1ee69a48ff14440c85dab6280519"
	assert_eq(lock.closure(a), [a], "closure of a leaf")
	lock.dependencies()[b]["requires"] = [a]
	assert_eq(lock.closure(b), [b, a], "closure includes requires")
	lock.remove_binding("prop-b")
	assert_true(not lock.bindings().has("prop-b"), "binding removed")
	# the world_generation root still references both keys, so nothing is pruned
	assert_true(lock.has_dependency(a) and lock.has_dependency(b), "world root keeps dependencies")
	lock.doc["roots"] = lock.doc["roots"].filter(func(r: Dictionary) -> bool: return r["owner_kind"] != "world_generation")
	lock.remove_binding("prop-a")
	assert_true(lock.dependencies().is_empty(), "unreferenced dependencies pruned: %s" % str(lock.dependencies().keys()))
	assert_true(lock.to_bytes().ok, "pruned lock still valid")


func test_remove_binding_keeps_shared_closure_member() -> void:
	var lock: RefCounted = Lock.parse_bytes(fixture("locks/valid/cluster_requires.json")).value
	var dep: String = "3e4cecabdb6c9bac29d5f9c655852e10f1d96abdb437cee386bfecf3974b1cbf"
	lock.add_binding("other", dep, "portable_glb_v1", {"mode": "preserve", "profile_id": null, "profile_sha256": null})
	lock.add_root("scene_binding", "other", lock.closure(dep))
	lock.remove_binding("cluster-1")
	assert_true(lock.has_dependency(dep), "dependency still reachable from another binding")
	assert_eq(lock.dependencies().size(), 1, "the cluster itself is pruned")


func test_binding_id_generation() -> void:
	var lock: RefCounted = _two_versions()
	var key: String = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
	assert_eq(lock.unique_binding_id("Oak Tree #1", key), "oak-tree-1-abcdef01", "slug + key prefix")
	lock.doc["bindings"]["oak-tree-1-abcdef01"] = {}
	assert_eq(lock.unique_binding_id("Oak Tree #1", key), "oak-tree-1-abcdef01-2", "uniquified")
	assert_eq(lock.unique_binding_id("", key), "asset-abcdef01", "fallback name")
	var long_id: String = lock.unique_binding_id("x".repeat(200), key)
	assert_true(long_id.length() <= 64 and _is_slug(long_id), "long names fit the slug limit: %s" % long_id)
	assert_eq(Lock.slugify("Čierny Smrek"), "ierny-smrek", "non-ascii dropped, no leading dash")


func _is_slug(id: String) -> bool:
	return load("res://addons/assetstudio/core/as_schema.gd").matches("slug", id)


func test_add_dependency_detects_conflicts() -> void:
	var lock: RefCounted = Lock.new_empty("0.1.0", "1.0.0")
	var AssetRef: GDScript = load("res://addons/assetstudio/core/as_asset_ref.gd")
	var ref: RefCounted = AssetRef.parse({"server_id": SERVER, "library_id": "prj_0000000000000001",
			"asset_id": "ast_00000000000000aa", "version_id": "ver_00000000000000v1"}).value
	var delivery: Dictionary = {"delivery_id": "dlv_00000000000000d1", "manifest_sha256": "a".repeat(64),
			"profile_id": "portable-default", "profile_version": "1.0.0"}
	assert_eq(lock.add_dependency(ref, "b".repeat(64), "portable_glb_v1", delivery, []), "", "first add")
	assert_eq(lock.add_dependency(ref, "b".repeat(64), "portable_glb_v1", delivery, []), "", "identical add")
	var other: Dictionary = delivery.duplicate()
	other["delivery_id"] = "dlv_00000000000000d2"
	assert_true(lock.add_dependency(ref, "b".repeat(64), "portable_glb_v1", other, []) != "", "different delivery")
	assert_true(lock.add_dependency(ref, "c".repeat(64), "portable_glb_v1", delivery, []) != "", "different descriptor")
	assert_true(lock.to_bytes().ok, "lock stays valid")


func test_validation_rules_beyond_the_fixtures() -> void:
	var base: Dictionary = JSON.parse_string(fixture("locks/valid/two_versions.json").get_string_from_utf8())
	var mutate: Array[Callable] = [
		func(d: Dictionary) -> void: d["bindings"]["prop-a"]["representation"] = "mobile_glb_v1",
		func(d: Dictionary) -> void: d["bindings"]["prop-a"]["material_policy"] = {"mode": "preserve", "profile_id": "x", "profile_sha256": "a".repeat(64)},
		func(d: Dictionary) -> void: d["bindings"]["prop-a"]["material_policy"] = {"mode": "project_mapping", "profile_id": null, "profile_sha256": null},
		func(d: Dictionary) -> void: d["bindings"]["prop-a"]["material_policy"]["profile_id"] = "x",
		func(d: Dictionary) -> void: d["roots"][0]["asset_keys"] = [],
		func(d: Dictionary) -> void: d["roots"][0]["asset_keys"] = ["f".repeat(64)],
		func(d: Dictionary) -> void: d["roots"][2]["asset_keys"].append(d["roots"][2]["asset_keys"][0]),
		func(d: Dictionary) -> void: d["roots"][0]["owner_kind"] = "other",
		func(d: Dictionary) -> void: d["dependencies"].values()[0]["requires"] = ["f".repeat(64)],
		func(d: Dictionary) -> void: d["extra"] = 1,
		func(d: Dictionary) -> void: d["generator"].erase("installer_version"),
	]
	for i: int in mutate.size():
		var d: Dictionary = base.duplicate(true)
		mutate[i].call(d)
		assert_true(Lock.validate(d) != "", "mutation %d must be invalid" % i)
	assert_eq(Lock.validate(base), "", "unmutated fixture is valid")


func test_needed_deliveries_cover_bindings_and_unbound_dependencies() -> void:
	var lock: RefCounted = _two_versions()
	assert_eq(lock.needed_deliveries().size(), 2, "two bound portable deliveries")
	lock.remove_binding("prop-b")
	assert_eq(lock.needed_deliveries().size(), 2, "world root still needs the unbound portable delivery")
