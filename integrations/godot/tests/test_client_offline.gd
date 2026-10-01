extends "res://tests/client_test_base.gd"

const DEAD_URL: String = "http://127.0.0.1:1"


func test_client_offline_cache_hit_after_server_unreachable() -> void:
	if not has_server("offline hit"):
		return
	var online: Dictionary = make_env()
	var first: RefCounted = await online["resolver"].prepare(make_ref())
	assert_true(first.ok, "online prepare: %s" % first.describe())
	var offline: Dictionary = make_env(DEAD_URL, "", online["cache"])
	offline["client"].max_retries = 1
	var r: RefCounted = await offline["resolver"].prepare(make_ref())
	assert_true(r.ok, "cache hit with unreachable server: %s" % r.describe())
	assert_eq(r.value["source"], "cache", "served from cache")
	assert_eq(FileAccess.get_sha256(r.value["files"]["portable.glb"]), GLB_V1_SHA, "bytes")
	offline["resolver"].offline_only = true
	assert_true((await offline["resolver"].prepare(make_ref())).ok, "explicit offline mode")
	await finish()


func test_client_offline_miss_never_falls_back_to_another_version() -> void:
	if not has_server("offline miss"):
		return
	var online: Dictionary = make_env()
	assert_true((await online["resolver"].prepare(make_ref(V1))).ok, "v1 cached")
	var offline: Dictionary = make_env(DEAD_URL, "", online["cache"])
	offline["client"].max_retries = 1
	var r: RefCounted = await offline["resolver"].prepare(make_ref(V2))
	assert_eq(r.code, "temporarily_unavailable", "v2 miss: %s" % r.describe())
	assert_true(not r.ok, "must not return v1 for v2")
	offline["resolver"].offline_only = true
	assert_eq((await offline["resolver"].prepare(make_ref(V2))).code, "temporarily_unavailable", "explicit offline miss")
	await finish()


func test_client_offline_partial_cache_is_a_miss() -> void:
	if not has_server("offline partial"):
		return
	var online: Dictionary = make_env()
	assert_true((await online["resolver"].prepare(make_ref())).ok, "cached")
	DirAccess.remove_absolute(online["cache"].blob_path(GLB_V1_SHA))
	var offline: Dictionary = make_env(DEAD_URL, "", online["cache"])
	offline["client"].max_retries = 0
	assert_eq((await offline["resolver"].prepare(make_ref())).code, "temporarily_unavailable", "missing blob")
	await finish()
