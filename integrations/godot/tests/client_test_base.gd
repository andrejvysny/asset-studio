extends "res://tests/test_case.gd"
# Shared setup for network tests. They run only under run_client_tests.py, which exports
# ASSETSTUDIO_FAKE_PORT / ASSETSTUDIO_FAKE_TOKEN; otherwise each test prints SKIP and passes neutrally.

const Registry = preload("res://addons/assetstudio/core/as_connection_registry.gd")
const BlobCache = preload("res://addons/assetstudio/core/as_blob_cache.gd")
const Client = preload("res://addons/assetstudio/core/as_library_client.gd")
const Resolver = preload("res://addons/assetstudio/core/as_asset_resolver.gd")
const AssetRef = preload("res://addons/assetstudio/core/as_asset_ref.gd")
const CancelToken = preload("res://addons/assetstudio/core/as_cancel_token.gd")
const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")

const SERVER_ID: String = "6f1c2a52-3c2e-4d4b-9a57-0b6f6f0c1d2e"
const LIBRARY: String = "prj_0000000000000001"
const ASSET: String = "ast_00000000000000aa"
const V1: String = "ver_00000000000000v1"
const V2: String = "ver_00000000000000v2"
const GLB_V1_SHA: String = "60b9ec7183bde3f53b6818bfb9771d90c2ba06f6ff6b47bd45ddd61214a056f4"
const GLB_V1_ARTIFACT: String = "art_v3j2qb6aa90np9ta"

var port: String = OS.get_environment("ASSETSTUDIO_FAKE_PORT")
var _token: String = OS.get_environment("ASSETSTUDIO_FAKE_TOKEN")
var _nodes: Array[Node] = []
var _dirs: Array[String] = []


func has_server(label: String) -> bool:
	if port == "":
		print("SKIP (no fake server): %s" % label)
		return false
	return true


func make_ref(version_id: String = V1) -> RefCounted:
	return AssetRef.parse({"server_id": SERVER_ID, "library_id": LIBRARY, "asset_id": ASSET, "version_id": version_id}).value


func new_dir(prefix: String) -> String:
	var dir: String = "user://as_test_%s_%d_%d" % [prefix, Time.get_ticks_usec(), randi()]
	_dirs.append(dir)
	return dir


## Returns {"registry", "cache", "client", "resolver"} pointing at the fake server (or `base_url`).
func make_env(base_url: String = "", token: String = "", cache: RefCounted = null) -> Dictionary:
	var reg: RefCounted = Registry.new(new_dir("reg"))
	reg.set_connection(SERVER_ID, base_url if base_url != "" else "http://127.0.0.1:%s" % port)
	reg.set_credential(SERVER_ID, token if token != "" else _token)
	var the_cache: RefCounted = cache if cache != null else BlobCache.new(new_dir("cache"))
	var client: Node = Client.new()
	client.backoff_base_s = 0.05
	client.setup(reg, SERVER_ID)
	var resolver: Node = Resolver.new()
	resolver.setup(client, the_cache)
	var root: Node = (Engine.get_main_loop() as SceneTree).root
	root.add_child(client)
	root.add_child(resolver)
	_nodes.append(client)
	_nodes.append(resolver)
	return {"registry": reg, "cache": the_cache, "client": client, "resolver": resolver}


func finish() -> void:
	await set_scenario("normal")
	if port != "":
		await control(HTTPClient.METHOD_POST, "/__reset_manifests")
	for n: Node in _nodes:
		n.queue_free()
	for d: String in _dirs:
		_remove_tree(d)


func _remove_tree(path: String) -> void:
	if not DirAccess.dir_exists_absolute(path):
		return
	for d: String in DirAccess.get_directories_at(path):
		_remove_tree(path.path_join(d))
	for f: String in DirAccess.get_files_at(path):
		DirAccess.remove_absolute(path.path_join(f))
	DirAccess.remove_absolute(path)


## Control-plane call to the fake server (unauthenticated endpoints).
func control(method: int, path: String, body: Dictionary = {}) -> Variant:
	var http := HTTPRequest.new()
	var root: Node = (Engine.get_main_loop() as SceneTree).root
	root.add_child(http)
	var headers: PackedStringArray = ["Content-Type: application/json"]
	http.request("http://127.0.0.1:%s%s" % [port, path], headers, method, JSON.stringify(body))
	var res: Array = await http.request_completed
	http.queue_free()
	return JSON.parse_string((res[3] as PackedByteArray).get_string_from_utf8())


func set_scenario(name: String) -> void:
	if port != "":
		await control(HTTPClient.METHOD_POST, "/__scenario", {"name": name})


func server_log() -> Array:
	return await control(HTTPClient.METHOD_GET, "/__log")


func clear_log() -> void:
	await control(HTTPClient.METHOD_POST, "/__log/clear")


func requests_to(log: Array, fragment: String) -> Array:
	return log.filter(func(e: Dictionary) -> bool: return str(e["path"]).contains(fragment))
