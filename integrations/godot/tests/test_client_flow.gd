extends "res://tests/client_test_base.gd"


func test_client_happy_path_prepare_exact_sha() -> void:
	if not has_server("happy path"):
		return
	var env: Dictionary = make_env()
	var states: Array[String] = []
	env["resolver"].state_changed.connect(func(_k: String, s: String, _p: float) -> void: states.append(s))
	var r: RefCounted = await env["resolver"].prepare(make_ref())
	assert_true(r.ok, "prepare: %s" % r.describe())
	if not r.ok:
		await finish()
		return
	assert_eq(r.value["source"], "network", "source")
	assert_eq(r.value["descriptor"].raw_sha256, "5b8517a39e3892184c5df6b733884265adae4289345cca8bd6172ffab15fb49b", "descriptor sha")
	var path: String = r.value["files"]["portable.glb"]
	assert_eq(FileAccess.get_sha256(path), GLB_V1_SHA, "blob sha256")
	assert_true(env["cache"].verify_blob(GLB_V1_SHA), "cache verify")
	assert_true(states.has("remote") and states.has("downloading") and states[-1] == "verified", "states %s" % str(states))
	var again: RefCounted = await env["resolver"].prepare(make_ref())
	assert_true(again.ok, "second prepare")
	await finish()


func test_client_metadata_calls() -> void:
	if not has_server("metadata"):
		return
	var env: Dictionary = make_env()
	var client: Node = env["client"]
	assert_true((await client.health()).ok, "health")
	var caps: RefCounted = await client.capabilities()
	assert_true(caps.ok and caps.value["server_id"] == SERVER_ID, "capabilities")
	var libs: RefCounted = await client.libraries()
	assert_true(libs.ok and libs.value["libraries"][0]["library_id"] == LIBRARY, "libraries")
	var ch: RefCounted = await client.changes("", 0.0)
	assert_true(ch.ok and ch.value["cursor"] == "Y3Vyc29y", "changes")
	await finish()


func test_client_unauthorized_and_unauthenticated_health() -> void:
	if not has_server("unauthorized"):
		return
	var env: Dictionary = make_env("", "wrong-token")
	assert_true((await env["client"].health()).ok, "health needs no token")
	var r: RefCounted = await env["client"].capabilities()
	assert_eq(r.code, "unauthorized", "bad token")
	await finish()


func test_client_rejects_unsafe_ids_without_network() -> void:
	if not has_server("id validation"):
		return
	var env: Dictionary = make_env()
	await clear_log()
	var client: Node = env["client"]
	for bad: String in ["../etc", "..", ".", "a/b", "a b", "", "%2e%2e", "a?x=1"]:
		assert_eq((await client.asset(bad, ASSET)).code, "invalid_request", "library %s" % bad)
		assert_eq((await client.version(LIBRARY, bad, V1)).code, "invalid_request", "asset %s" % bad)
		assert_eq((await client.manifest_bytes(LIBRARY, bad)).code, "invalid_request", "delivery %s" % bad)
	assert_true((await server_log()).is_empty(), "no request was sent")
	await finish()


func test_client_unknown_version_is_not_substituted() -> void:
	if not has_server("unknown version"):
		return
	var env: Dictionary = make_env()
	var r: RefCounted = await env["resolver"].prepare(make_ref("ver_00000000000000v9"))
	assert_eq(r.code, "version_unavailable", "typed passthrough")
	await finish()


func test_client_forbidden() -> void:
	if not has_server("forbidden"):
		return
	var env: Dictionary = make_env()
	await set_scenario("forbidden")
	var r: RefCounted = await env["resolver"].prepare(make_ref())
	assert_eq(r.code, "forbidden", "forbidden")
	await finish()


func test_client_wrong_server_id_stops_before_content() -> void:
	if not has_server("wrong server id"):
		return
	var env: Dictionary = make_env()
	await set_scenario("wrong_server_id")
	await clear_log()
	var r: RefCounted = await env["resolver"].prepare(make_ref())
	assert_eq(r.code, "server_identity_mismatch", "identity")
	var log: Array = await server_log()
	assert_true(requests_to(log, "/resolve").is_empty(), "no resolve sent")
	assert_true(requests_to(log, "/content").is_empty() and requests_to(log, "/manifest").is_empty(), "no content sent")
	var dl: RefCounted = await env["client"].download_artifact(LIBRARY, GLB_V1_ARTIFACT, env["cache"].staging_path(GLB_V1_SHA), GLB_V1_SHA, 956)
	assert_eq(dl.code, "server_identity_mismatch", "download also refused")
	assert_true(requests_to(await server_log(), "/content").is_empty(), "still no content request")
	await finish()


func test_client_changes_watcher_tracks_cursor() -> void:
	if not has_server("change watcher"):
		return
	var env: Dictionary = make_env()
	var watcher: Node = load("res://addons/assetstudio/core/as_change_watcher.gd").new()
	watcher.poll_timeout_s = 0.0
	(Engine.get_main_loop() as SceneTree).root.add_child(watcher)
	_nodes.append(watcher)
	watcher.start(env["client"])
	await (Engine.get_main_loop() as SceneTree).create_timer(0.6).timeout
	watcher.stop()
	assert_eq(watcher.cursor, "Y3Vyc29y", "opaque cursor stored")
	await finish()
