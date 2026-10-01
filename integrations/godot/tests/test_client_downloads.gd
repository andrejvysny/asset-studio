extends "res://tests/client_test_base.gd"


func _partial(env: Dictionary, bytes: PackedByteArray) -> String:
	var path: String = env["cache"].staging_path(GLB_V1_SHA)
	DirAccess.make_dir_recursive_absolute(path.get_base_dir())
	var f: FileAccess = FileAccess.open(path, FileAccess.WRITE)
	f.store_buffer(bytes)
	f.close()
	return path


func _glb_prefix(n: int) -> PackedByteArray:
	var dir: String = OS.get_environment("ASSETSTUDIO_CONTRACTS_DIR")
	return FileAccess.get_file_as_bytes(dir.path_join("fixtures/glb/primitive_prop.portable.glb")).slice(0, n)


func test_client_corruption_is_integrity_mismatch() -> void:
	if not has_server("corruption"):
		return
	var env: Dictionary = make_env()
	await set_scenario("corrupt_content")
	var r: RefCounted = await env["resolver"].prepare(make_ref())
	assert_eq(r.code, "integrity_mismatch", "code")
	assert_true(not env["cache"].has_blob(GLB_V1_SHA), "no blob installed")
	assert_true(not FileAccess.file_exists(env["cache"].staging_path(GLB_V1_SHA)), "corrupt staging deleted")
	await finish()


func test_client_resumes_with_range_after_dropped_connection() -> void:
	if not has_server("range resume"):
		return
	var env: Dictionary = make_env()
	env["client"].max_retries = 0
	await set_scenario("drop_midway")
	var failed: RefCounted = await env["resolver"].prepare(make_ref())
	assert_true(not failed.ok, "dropped download must fail")
	var staged: String = env["cache"].staging_path(GLB_V1_SHA)
	var partial_size: int = FileAccess.get_file_as_bytes(staged).size()
	assert_true(partial_size > 0 and partial_size < 956, "partial bytes kept (%d)" % partial_size)
	await set_scenario("normal")
	await clear_log()
	var r: RefCounted = await env["resolver"].prepare(make_ref())
	assert_true(r.ok, "resume: %s" % r.describe())
	var ranges: Array = (await server_log()).filter(func(e: Dictionary) -> bool: return e["range"] != null)
	assert_eq(ranges.size(), 1, "one Range request")
	assert_eq(ranges[0]["range"] if not ranges.is_empty() else "", "bytes=%d-" % partial_size, "range offset")
	assert_eq(FileAccess.get_sha256(env["cache"].blob_path(GLB_V1_SHA)), GLB_V1_SHA, "final bytes")
	await finish()


func test_client_resume_appends_valid_prefix() -> void:
	if not has_server("valid prefix"):
		return
	var env: Dictionary = make_env()
	_partial(env, _glb_prefix(300))
	var r: RefCounted = await env["resolver"].prepare(make_ref())
	assert_true(r.ok, "resume from valid prefix: %s" % r.describe())
	assert_eq(FileAccess.get_sha256(env["cache"].blob_path(GLB_V1_SHA)), GLB_V1_SHA, "final bytes")
	await finish()


func test_client_ignored_range_restarts_instead_of_appending() -> void:
	if not has_server("ignore range"):
		return
	var env: Dictionary = make_env()
	var garbage := PackedByteArray()
	garbage.resize(100)
	garbage.fill(0xAA)
	_partial(env, garbage)  # appending to this would corrupt the file; a safe restart fixes it
	await set_scenario("ignore_range")
	await clear_log()
	var r: RefCounted = await env["resolver"].prepare(make_ref())
	assert_true(r.ok, "restart: %s" % r.describe())
	var ranges: Array = (await server_log()).filter(func(e: Dictionary) -> bool: return e["range"] != null)
	assert_true(not ranges.is_empty(), "client did attempt a Range request")
	assert_eq(FileAccess.get_sha256(env["cache"].blob_path(GLB_V1_SHA)), GLB_V1_SHA, "final bytes correct")
	await finish()


func test_client_cancel_during_slow_download() -> void:
	if not has_server("cancel"):
		return
	var env: Dictionary = make_env()
	await set_scenario("slow")
	var token: RefCounted = CancelToken.new()
	(Engine.get_main_loop() as SceneTree).create_timer(0.7).timeout.connect(token.cancel)
	var states: Array[String] = []
	env["resolver"].state_changed.connect(func(_k: String, s: String, _p: float) -> void: states.append(s))
	var started: int = Time.get_ticks_msec()
	var r: RefCounted = await env["resolver"].prepare(make_ref(), "portable_glb_v1", token)
	assert_eq(r.code, "cancelled", "cancelled: %s" % r.describe())
	assert_true(Time.get_ticks_msec() - started < 5000, "cancel was prompt")
	assert_true(not env["cache"].has_blob(GLB_V1_SHA), "nothing installed")
	assert_eq(states[-1] if not states.is_empty() else "", "cancelled", "state")
	await finish()


func _spawn_download(env: Dictionary, index: int, done: Array) -> void:
	var path: String = env["cache"].staging_path(GLB_V1_SHA) + str(index)
	var r: RefCounted = await env["client"].download_artifact(LIBRARY, GLB_V1_ARTIFACT, path, GLB_V1_SHA, 956)
	done.append(r.ok)


func test_client_downloads_are_limited_to_two_concurrent() -> void:
	if not has_server("concurrent"):
		return
	var env: Dictionary = make_env()
	var done: Array = []
	for i: int in 6:
		_spawn_download(env, i, done)
	while done.size() < 6:
		await (Engine.get_main_loop() as SceneTree).process_frame
	assert_true(not done.has(false), "all downloads succeed")
	assert_eq(env["client"].peak_downloads, 2, "never more than 2 in flight, and 2 were used")
	await finish()
