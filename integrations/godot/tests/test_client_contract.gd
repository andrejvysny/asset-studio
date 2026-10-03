extends "res://tests/client_test_base.gd"
# Capability enforcement, resolve `representations`, and pinned cache-first serving.

const Schema = preload("res://addons/assetstudio/core/as_schema.gd")
const AddCommand = preload("res://addons/assetstudio/project/as_add_command.gd")
const DEAD_URL: String = "http://127.0.0.1:1"


func _mutate(version_id: String, caps: Array, dependency_on: String = "") -> void:
	var body: Dictionary = {"version_id": version_id, "required_capabilities": caps}
	if dependency_on != "":
		body["dependency_on"] = dependency_on
	await control(HTTPClient.METHOD_POST, "/__mutate", body)


func test_client_supported_capabilities_match_contract() -> void:
	var dir: String = OS.get_environment("ASSETSTUDIO_CONTRACTS_DIR")
	if dir == "":
		print("SKIP (no contracts dir): capabilities drift")
		return
	var doc: Variant = JSON.parse_string(FileAccess.get_file_as_string(dir.path_join("capabilities.json")))
	var known: Array = (doc as Dictionary)["known_capabilities"]
	var ours: Array = Array(Schema.SUPPORTED_CAPABILITIES)
	known.sort()
	ours.sort()
	assert_eq(ours, known, "SUPPORTED_CAPABILITIES drifted from capabilities.json")
	var syntax: Array = Array(Schema.CAPABILITIES)
	syntax.sort()
	assert_eq(syntax, known, "CAPABILITIES drifted from capabilities.json")


func test_client_unsatisfiable_capability_on_root_is_refused() -> void:
	if not has_server("caps root"):
		return
	await _mutate(V1, ["alpha_blend"])
	var env: Dictionary = make_env()
	env["resolver"].supported_capabilities = PackedStringArray(["pbr_textures"])
	await clear_log()
	var r: RefCounted = await env["resolver"].prepare(make_ref(V1))
	assert_eq(r.code, "unsupported_contract", "code: %s" % r.describe())
	assert_true(r.message.contains("alpha_blend") and r.message.contains(make_ref(V1).key()), "message names capability and key")
	assert_true(requests_to(await server_log(), "/content").is_empty(), "no content download")
	assert_true(not env["cache"].has_blob(GLB_V1_SHA), "nothing materialized")
	var ok_env: Dictionary = make_env()  # default set satisfies alpha_blend
	assert_true((await ok_env["resolver"].prepare(make_ref(V1))).ok, "satisfiable capability passes")
	await finish()


func test_client_unsatisfiable_capability_on_dependency_fails_closure() -> void:
	if not has_server("caps dependency"):
		return
	await _mutate(V1, ["alpha_blend"])
	await _mutate(V2, [], V1)
	var env: Dictionary = make_env()
	var lone: RefCounted = await env["resolver"].prepare(make_ref(V2))
	assert_true(lone.ok and lone.value["manifest"].data["dependencies"].size() == 1, "root alone is fine: %s" % lone.describe())
	env["resolver"].supported_capabilities = PackedStringArray()
	var closure: RefCounted = await AddCommand.resolve_closure(env["resolver"], make_ref(V2))
	assert_eq(closure.code, "unsupported_contract", "closure: %s" % closure.describe())
	assert_true(closure.message.contains(make_ref(V1).key()), "message names the dependency asset_key")
	var ok_env: Dictionary = make_env()
	assert_true((await AddCommand.resolve_closure(ok_env["resolver"], make_ref(V2))).ok, "satisfiable closure resolves")
	await finish()


func test_client_representations_not_ready_returns_its_error() -> void:
	if not has_server("rep not ready"):
		return
	var env: Dictionary = make_env()
	await set_scenario("rep_unsupported")
	await clear_log()
	var r: RefCounted = await env["resolver"].prepare(make_ref())
	assert_eq(r.code, "unsupported_representation", "code: %s" % r.describe())
	assert_true(r.message.contains("no preparer"), "server message kept")
	assert_true(requests_to(await server_log(), "/manifest").is_empty(), "no manifest fetched")
	await set_scenario("rep_unsupported_no_error")
	assert_eq((await env["resolver"].prepare(make_ref())).code, "unsupported_representation", "null error fallback")
	await finish()


func test_client_representations_missing_key_is_unsupported() -> void:
	if not has_server("rep missing key"):
		return
	var env: Dictionary = make_env()
	await set_scenario("rep_missing_key")
	var r: RefCounted = await env["resolver"].prepare(make_ref())
	assert_eq(r.code, "unsupported_representation", "code: %s" % r.describe())
	await finish()


func test_client_legacy_resolve_without_representations_still_works() -> void:
	if not has_server("legacy resolve"):
		return
	var env: Dictionary = make_env()
	await set_scenario("legacy_resolve")
	var r: RefCounted = await env["resolver"].prepare(make_ref())
	assert_true(r.ok and r.value["source"] == "network", "legacy entry: %s" % r.describe())
	await finish()


func test_client_pinned_prepare_is_cache_first() -> void:
	if not has_server("pinned cache-first"):
		return
	var env: Dictionary = make_env()
	var first: RefCounted = await env["resolver"].prepare(make_ref())
	assert_true(first.ok, "seed cache")
	var pin: String = first.value["delivery_id"]
	await clear_log()
	var hit: RefCounted = await env["resolver"].prepare(make_ref(), "portable_glb_v1", null, pin)
	assert_true(hit.ok and hit.value["source"] == "cache", "pinned from cache: %s" % hit.describe())
	assert_eq(FileAccess.get_sha256(hit.value["files"]["portable.glb"]), GLB_V1_SHA, "bytes")
	assert_true((await server_log()).is_empty(), "no network request")
	await set_scenario("forbidden")  # unpinned stays network-first
	assert_eq((await env["resolver"].prepare(make_ref())).code, "forbidden", "unpinned prepare asks the server")
	assert_true((await env["resolver"].prepare(make_ref(), "portable_glb_v1", null, pin)).ok, "pinned ignores server errors")
	var dead: Dictionary = make_env(DEAD_URL, "", env["cache"])
	dead["client"].max_retries = 0
	assert_true((await dead["resolver"].prepare(make_ref(), "portable_glb_v1", null, pin)).ok, "pinned with unreachable server")
	await finish()


func test_client_pinned_corrupt_blob_falls_back_to_network() -> void:
	if not has_server("pinned corrupt blob"):
		return
	var env: Dictionary = make_env()
	var first: RefCounted = await env["resolver"].prepare(make_ref())
	var pin: String = first.value["delivery_id"]
	var path: String = env["cache"].blob_path(GLB_V1_SHA)
	var size: int = FileAccess.get_file_as_bytes(path).size()
	var f: FileAccess = FileAccess.open(path, FileAccess.WRITE)
	f.store_buffer(_filler(size))
	f.close()
	assert_true(not env["cache"].verify_blob(GLB_V1_SHA), "blob is corrupt, same size")
	var dead: Dictionary = make_env(DEAD_URL, "", env["cache"])
	dead["client"].max_retries = 0
	var miss: RefCounted = await dead["resolver"].prepare(make_ref(), "portable_glb_v1", null, pin)
	assert_true(not miss.ok, "corrupt cache and no server: never returns bytes")
	var healed: RefCounted = await env["resolver"].prepare(make_ref(), "portable_glb_v1", null, pin)
	assert_true(healed.ok and healed.value["source"] == "network", "re-downloaded: %s" % healed.describe())
	assert_eq(FileAccess.get_sha256(healed.value["files"]["portable.glb"]), GLB_V1_SHA, "verified bytes")
	await finish()


func _filler(n: int) -> PackedByteArray:
	var b := PackedByteArray()
	b.resize(n)
	b.fill(0x41)
	return b
