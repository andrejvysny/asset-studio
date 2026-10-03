@tool
extends RefCounted
# CLI command implementations (design §9). One instance per CLI run; `host` is the node that owns the network
# nodes (the SceneTree root). Every command returns an exit code: 0 ok, 1 failure, 2 usage error.
# Messages never contain credentials (results carry no headers; tokens are only read from --token-file).

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")
const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")
const Version = preload("res://addons/assetstudio/core/as_version.gd")
const Registry = preload("res://addons/assetstudio/core/as_connection_registry.gd")
const BlobCache = preload("res://addons/assetstudio/core/as_blob_cache.gd")
const Client = preload("res://addons/assetstudio/core/as_library_client.gd")
const Resolver = preload("res://addons/assetstudio/core/as_asset_resolver.gd")
const Config = preload("res://addons/assetstudio/project/as_project_config.gd")
const Lock = preload("res://addons/assetstudio/project/as_project_lock.gd")
const Coordinator = preload("res://addons/assetstudio/project/as_mutation_coordinator.gd")
const Installer = preload("res://addons/assetstudio/project/as_installer.gd")
const Restore = preload("res://addons/assetstudio/project/as_restore.gd")
const State = preload("res://addons/assetstudio/project/as_project_state.gd")
const AddCommand = preload("res://addons/assetstudio/project/as_add_command.gd")
const Finalize = preload("res://addons/assetstudio/project/as_finalize.gd")
const UpdateCommand = preload("res://addons/assetstudio/project/as_update_command.gd")
const PolicyCommand = preload("res://addons/assetstudio/project/as_policy_command.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")
const Manifest = preload("res://addons/assetstudio/core/as_delivery_manifest.gd")
const ExportPreflight = preload("res://addons/assetstudio/project/as_export_preflight.gd")
const PruneCommand = preload("res://addons/assetstudio/project/as_prune_command.gd")
const PublishCommand = preload("res://addons/assetstudio/project/as_publish_command.gd")

var root: String = ""
var registry: RefCounted = null
var cache: RefCounted = null
## Most recently created client (null for offline runs).
var client: Node = null

var _host: Node = null
var _nodes: Array[Node] = []


## `registry_dir` / `cache_dir` default to user://assetstudio[/cache]; tests pass isolated directories.
func _init(host: Node, project_root: String, registry_dir: String = "", cache_dir: String = "") -> void:
	_host = host
	root = project_root.simplify_path().trim_suffix("/")
	registry = Registry.new(registry_dir) if registry_dir != "" else Registry.new()
	cache = BlobCache.new(cache_dir) if cache_dir != "" else BlobCache.new()


func run(command: String, opts: Dictionary) -> int:
	var code: int = 1
	match command:
		"connect":
			code = await _connect(opts)
		"restore":
			code = await _restore(opts)
		"verify":
			code = _verify()
		"add":
			code = await AddCommand.run(self, opts)
		"finalize":
			code = Finalize.run(self, opts)
		"set-policy":
			code = PolicyCommand.run(self, opts)
		"update":
			code = await UpdateCommand.run_update(self, opts)
		"rollback":
			code = await UpdateCommand.run_rollback(self, opts)
		"publish":
			code = await PublishCommand.run(self, opts)
		"prune-deliveries":
			code = PruneCommand.run(self, opts)
		"export-preflight":
			code = _export_preflight(opts)
		_:
			err("unknown command: %s" % command)
			code = 2
	release()
	return code


## The node that owns the network nodes (and hosts the publish graph).
func host() -> Node:
	return _host


## Frees the client/resolver nodes created by make_client / make_resolver.
func release() -> void:
	for n: Node in _nodes:
		n.queue_free()
	_nodes.clear()


func say(msg: String) -> void:
	print(msg)


func err(msg: String) -> void:
	printerr("error: %s" % msg)


func fail_exit(r: RefCounted) -> int:
	err(r.describe())
	for p: Variant in (r.details as Dictionary).get("problems", []):
		printerr("  - %s" % str(p))
	return 1


## Opens a coordinator transaction (mutex + crash recovery); null after printing the reason.
func open_txn(operation: String) -> RefCounted:
	var c: RefCounted = Coordinator.new(root)
	var r: RefCounted = c.open(operation)
	for n: String in c.notes:
		say("note: %s" % n)
	if not r.ok:
		fail_exit(r)
		return null
	return c


## Resolver wired to the registry's server (or cache-only when `offline`, which creates no client at all).
func make_resolver(server_id: String, offline: bool) -> Node:
	var resolver: Node = Resolver.new()
	if offline:
		resolver.offline_only = true
		resolver.setup(null, cache)
	else:
		resolver.setup(make_client(server_id), cache)
	_host.add_child(resolver)
	_nodes.append(resolver)
	return resolver


func make_client(server_id: String) -> Node:
	var c: Node = Client.new()
	c.setup(registry, server_id)
	_host.add_child(c)
	_nodes.append(c)
	client = c
	return c


## Pins every blob of every locked delivery (that the cache still knows) so cache pruning cannot drop them.
func pin_lock(lock: RefCounted) -> void:
	var shas := PackedStringArray()
	for need: Dictionary in lock.call("needed_deliveries"):
		var dep: Dictionary = (lock.call("dependencies") as Dictionary)[need["key"]]
		var msha: String = dep["deliveries"][need["representation"]]["manifest_sha256"]
		var man: RefCounted = Manifest.parse_bytes(cache.load_document("manifests", msha))
		if man.ok:
			for f: Dictionary in man.value.data["files"]:
				shas.append(f["sha256"])
	cache.pin("lock:" + Canonical.sha256_hex(root.to_utf8_buffer()).left(12), shas)


# --- connect -------------------------------------------------------------------------------------------------

func _connect(o: Dictionary) -> int:
	var path: String = o["token-file"]
	if not FileAccess.file_exists(path):
		err("token file not found")
		return 2
	var token: String = FileAccess.get_file_as_string(path).strip_edges()
	var server_id: String = o["server-id"]
	var r: RefCounted = registry.set_connection(server_id, o["url"], o.has("allow-insecure-lan"))
	if r.ok:
		r = registry.set_credential(server_id, token)
	if not r.ok:
		err(r.describe())
		return 2
	var caps: RefCounted = await make_client(server_id).capabilities()
	if not caps.ok:
		registry.remove_connection(server_id)
		return fail_exit(caps)
	var c: RefCounted = open_txn("connect")
	if c == null:
		return 1
	var cfg: RefCounted = _config_for_connect(server_id)
	if not cfg.ok:
		c.close()
		return fail_exit(cfg)
	if cfg.value != null:
		c.add_write(Config.FILE_NAME, (cfg.value.call("to_bytes") as RefCounted).value)
	r = c.commit()
	c.close()
	if not r.ok:
		return fail_exit(r)
	say("connected to server %s" % server_id)
	return 0


## value = new ASProjectConfig to write, or null when the project file already matches.
func _config_for_connect(server_id: String) -> RefCounted:
	if not FileAccess.file_exists(root.path_join(Config.FILE_NAME)):
		return Result.success(Config.defaults(server_id))
	var cur: RefCounted = Config.load_from(root)
	if not cur.ok:
		return cur
	if cur.value.get("server_id") != server_id:
		return Result.fail("server_identity_mismatch", "this project is bound to server %s" % cur.value.get("server_id"))
	return Result.success(null)


# --- restore / verify -----------------------------------------------------------------------------

func load_locked() -> RefCounted:
	var cfg: RefCounted = Config.load_from(root)
	if not cfg.ok:
		return cfg
	var path: String = root.path_join(Lock.FILE_NAME)
	if not FileAccess.file_exists(path):
		return Result.fail(Result.CODE_INVALID_PROJECT_FILE, "%s not found" % Lock.FILE_NAME)
	var lock: RefCounted = Lock.parse_bytes(Fs.read_bytes(path))
	if not lock.ok:
		return lock
	return Result.success({"config": cfg.value, "lock": lock.value})


func _restore(o: Dictionary) -> int:
	var c: RefCounted = open_txn("restore")
	if c == null:
		return 1
	var loaded: RefCounted = load_locked()
	if not loaded.ok:
		c.close()
		return fail_exit(loaded)
	var lock: RefCounted = loaded.value["lock"]
	var cfg: RefCounted = loaded.value["config"]
	for dep: Dictionary in (lock.call("dependencies") as Dictionary).values():
		if dep["asset_ref"]["server_id"] != cfg.get("server_id"):
			c.close()
			err("lock references a server other than the project's server")
			return 1
	var resolver: Node = make_resolver(cfg.get("server_id"), o.has("offline"))
	var r: RefCounted = await Restore.restore(resolver, lock, cfg, c, {"trust_shaders": o.has("trust-shaders")})
	if not r.ok:
		c.close()
		return fail_exit(r)
	var counts: Dictionary = r.value
	var cr: RefCounted = c.commit() if counts["installed"] > 0 else Result.success()
	c.close()
	if not cr.ok:
		return fail_exit(cr)
	pin_lock(lock)
	say("restored: %d installed, %d already present" % [counts["installed"], counts["present"]])
	return 0


func _verify() -> int:
	var rec: RefCounted = Coordinator.recover_project(root)
	if not rec.ok:
		return fail_exit(rec)
	var loaded: RefCounted = load_locked()
	if not loaded.ok:
		return fail_exit(loaded)
	var r: RefCounted = Restore.verify_locked(root, loaded.value["lock"], loaded.value["config"])
	if not r.ok:
		return fail_exit(r)
	say("verified %d delivery(ies)" % r.value["checked"])
	return 0


## One JSON report on stdout (sorted keys); exit 1 when it has problems. Offline by construction.
func _export_preflight(o: Dictionary) -> int:
	var report: Dictionary = ExportPreflight.run(root, str(o.get("preset", "")), o.has("offline"))
	say(JSON.stringify(report, "", true))
	return 0 if report["ok"] else 1
