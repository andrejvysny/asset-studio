@tool
extends RefCounted
# `add --library L --asset A --version V [--binding id] [--profile pid | --preserve]` (design §9).
# Resolves the exact reference (plus its manifest dependency closure), installs the deliveries, adds dependency +
# binding + root to the lock and writes a wrapper scene WITHOUT material overrides, all in ONE coordinator
# transaction. The binding is marked pending_import until `finalize` runs after the headless import (AS-08 applies
# material policies there).

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")
const Version = preload("res://addons/assetstudio/core/as_version.gd")
const AssetRef = preload("res://addons/assetstudio/core/as_asset_ref.gd")
const Config = preload("res://addons/assetstudio/project/as_project_config.gd")
const Lock = preload("res://addons/assetstudio/project/as_project_lock.gd")
const Installer = preload("res://addons/assetstudio/project/as_installer.gd")
const Wrapper = preload("res://addons/assetstudio/project/as_wrapper.gd")
const State = preload("res://addons/assetstudio/project/as_project_state.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")

const REPRESENTATION: String = "portable_glb_v1"
const SOURCE_REPRESENTATION: String = "godot_static_source_v1"


## CLI entry. `cmd` is the ASCommands instance (project root, registry, cache, helpers).
static func run(cmd: RefCounted, o: Dictionary) -> int:
	var r: RefCounted = await execute(cmd, o)
	if r.ok:
		return 0
	if r.details.get("usage", false):
		cmd.err(r.message)
		return 2
	return cmd.fail_exit(r)


## Shared by the CLI and the dock. value = {"binding_id", "asset_key"}. Usage errors carry details.usage = true.
static func execute(cmd: RefCounted, o: Dictionary) -> RefCounted:
	var cfg: RefCounted = _load_config(cmd)
	if not cfg.ok:
		return cfg
	var config: RefCounted = cfg.value["config"]
	var ref_r: RefCounted = AssetRef.parse({"server_id": config.get("server_id"), "library_id": o["library"],
			"asset_id": o["asset"], "version_id": o["version"]})
	if not ref_r.ok:
		return Result.fail("invalid_request", ref_r.message, false, {"usage": true})
	if o.has("binding") and not Schema.matches("slug", o["binding"]):
		return Result.fail("invalid_request", "--binding must be a slug: 1-64 chars of a-z 0-9 _ . - starting with a letter or digit", false, {"usage": true})
	var rep_err: String = _representation_error(o)
	if rep_err != "":
		return Result.fail("invalid_request", rep_err, false, {"usage": true})
	var policy: RefCounted = _material_policy(cmd, o, config)
	if not policy.ok:
		return policy
	var c: RefCounted = cmd.open_txn("add")
	if c == null:
		return Result.fail(Result.CODE_LOCKED, "cannot open a transaction")
	var r: RefCounted = await _add(cmd, c, o, cfg.value, ref_r.value, policy.value)
	c.close()
	return r


## "" when the requested representation (default portable_glb_v1) can be added with these options.
static func _representation_error(o: Dictionary) -> String:
	var rep: String = o.get("representation", REPRESENTATION)
	if rep != REPRESENTATION and rep != SOURCE_REPRESENTATION:
		return "--representation must be portable_glb_v1 or godot_static_source_v1"
	if rep == SOURCE_REPRESENTATION and (o.has("profile") or o.has("override")):
		return "material profiles apply to portable_glb_v1 bindings only"
	if rep != SOURCE_REPRESENTATION and o.has("trust-shaders"):
		return "--trust-shaders applies to godot_static_source_v1 only"
	return ""


static func _load_config(cmd: RefCounted) -> RefCounted:
	var r: RefCounted = Config.load_from(cmd.root)
	if not r.ok:
		return r
	var lock: RefCounted = Lock.new_empty(Version.VERSION, Installer.INSTALLER_VERSION)
	var path: String = cmd.root.path_join(Lock.FILE_NAME)
	if FileAccess.file_exists(path):
		lock = Lock.parse_bytes(Fs.read_bytes(path))
		if not lock.ok:
			return lock
		lock = lock.value
	return Result.success({"config": r.value, "lock": lock})


static func _material_policy(cmd: RefCounted, o: Dictionary, config: RefCounted) -> RefCounted:
	var profile_id: Variant = null
	if o.has("profile"):
		profile_id = o["profile"]
	elif not o.has("preserve") and config.get("default_material_policy")["mode"] == "project_mapping":
		profile_id = config.get("default_material_policy")["profile_id"]
	if profile_id == null:
		return Result.success({"mode": "preserve", "profile_id": null, "profile_sha256": null})
	if not Schema.matches("slug", str(profile_id)):
		return Result.fail("invalid_request", "profile id must be a slug")
	var file: String = Fs.res_to_abs(cmd.root, config.get("material_profiles_dir")).path_join("%s.json" % profile_id)
	if not FileAccess.file_exists(file):
		return Result.fail(Result.CODE_INVALID_PROJECT_FILE, "material profile %s not found" % profile_id)
	return Result.success({"mode": "project_mapping", "profile_id": profile_id,
			"profile_sha256": Fs.sha256_file(file)})


static func _add(cmd: RefCounted, c: RefCounted, o: Dictionary, loaded: Dictionary, ref: RefCounted,
		policy: Dictionary) -> RefCounted:
	var config: RefCounted = loaded["config"]
	var lock: RefCounted = loaded["lock"]
	var key: String = ref.call("key")
	for existing: String in lock.call("bindings"):
		if lock.call("bindings")[existing]["asset_key"] == key:
			return Result.fail("invalid_request", "this exact version is already bound as %s" % existing)
	var resolver: Node = cmd.make_resolver(config.get("server_id"), false)
	var rep: String = o.get("representation", REPRESENTATION)
	var nodes: RefCounted = await resolve_closure(resolver, ref, rep)
	if not nodes.ok:
		return nodes
	var hint: String = await _name_hint(cmd.client, ref)
	var bid: String = o["binding"] if o.has("binding") else lock.call("unique_binding_id", hint, key)
	var applied: RefCounted = apply_binding(c, config, lock, nodes.value, bid, policy, rep,
			{"trust_shaders": o.has("trust-shaders")})
	if not applied.ok:
		return applied
	c.summary = {"kind": "add", "binding_id": bid, "to_key": key}
	var committed: RefCounted = c.commit()
	if not committed.ok:
		return committed
	cmd.pin_lock(lock)
	cmd.say("added %s (%s): run `godot --headless --editor --path . --import`, then `finalize`" % [bid, key.left(8)])
	return Result.success({"binding_id": bid, "asset_key": key})


## Asset display name for the binding id; falls back to the asset id when the server has no detail route.
static func _name_hint(client: Node, ref: RefCounted) -> String:
	var detail: RefCounted = await client.asset(ref.get("library_id"), ref.get("asset_id"))
	if detail.ok and detail.value is Dictionary and (detail.value as Dictionary).get("name") is String:
		return (detail.value as Dictionary)["name"]
	return ref.get("asset_id")


## Prepares the root and, breadth first, every manifest dependency at its pinned delivery.
## value = [{"ref", "prep", "requires": [asset_key]}], root first.
static func resolve_closure(resolver: Node, root_ref: RefCounted, root_rep: String = REPRESENTATION) -> RefCounted:
	var out: Array = []
	var seen: Dictionary = {}
	var queue: Array = [{"ref": root_ref, "rep": root_rep, "pin": "", "man": "", "desc": ""}]
	while not queue.is_empty():
		var item: Dictionary = queue.pop_front()
		var key: String = item["ref"].call("key")
		if seen.has(key):
			continue
		seen[key] = true
		var prep: RefCounted = await resolver.prepare(item["ref"], item["rep"], null, item["pin"])
		if not prep.ok:
			return prep
		var v: Dictionary = prep.value
		if item["pin"] != "" and (v["manifest"].get("raw_sha256") != item["man"] or v["descriptor"].get("raw_sha256") != item["desc"]):
			return Result.fail("integrity_mismatch", "dependency does not match the manifest that requires it")
		var requires: Array = []
		for dep: Dictionary in v["manifest"].data["dependencies"]:
			requires.append(dep["asset_key"])
			queue.append({"ref": AssetRef.parse(dep["asset_ref"]).value, "rep": dep["representation"],
					"pin": dep["delivery_id"], "man": dep["manifest_sha256"], "desc": dep["descriptor_sha256"]})
		out.append({"ref": item["ref"], "prep": v, "requires": requires})
	return Result.success(out)


## Queues every file operation on `c` and updates `lock` in place.
static func apply_binding(c: RefCounted, config: RefCounted, lock: RefCounted, nodes: Array, bid: String,
		policy: Dictionary, rep: String = REPRESENTATION, opts: Dictionary = {}) -> RefCounted:
	var inst: RefCounted = install_nodes(c, config, lock, nodes, opts)
	if not inst.ok:
		return inst
	var root_node: Dictionary = nodes[0]
	var key: String = root_node["ref"].call("key")
	var err: String = lock.call("add_binding", bid, key, rep, policy)
	if err != "":
		return Result.fail("invalid_request", err)
	lock.call("add_root", "scene_binding", bid, lock.call("closure", key))
	return _queue_files(c, config, lock, root_node, bid, rep != SOURCE_REPRESENTATION)


## Installs every resolved node and records it as a lock dependency (no binding yet). `opts` carries the source
## install options (trust_shaders); a source node's asset dependencies must be part of `nodes` (the closure).
static func install_nodes(c: RefCounted, config: RefCounted, lock: RefCounted, nodes: Array,
		opts: Dictionary = {}) -> RefCounted:
	var closure: Array = nodes.map(func(n: Dictionary) -> String: return n["ref"].call("key"))
	for n: Dictionary in nodes:
		var source_opts: Dictionary = opts.duplicate()
		source_opts["closure_keys"] = closure
		var inst: RefCounted = Installer.install(c, config.call("managed_rel"), n["ref"], n["prep"], source_opts)
		if not inst.ok:
			return inst
		n["entry_rel"] = inst.value["entry_rel"]
		var m: RefCounted = n["prep"]["manifest"]
		var delivery: Dictionary = {"delivery_id": n["prep"]["delivery_id"], "manifest_sha256": m.get("raw_sha256"),
				"profile_id": m.data["profile_id"], "profile_version": m.data["profile_version"]}
		var conflict: String = lock.call("add_dependency", n["ref"], n["prep"]["descriptor"].get("raw_sha256"),
				m.data["representation"], delivery, n["requires"])
		if conflict != "":
			return Result.fail("integrity_mismatch", conflict)
	return Result.success()


static func _queue_files(c: RefCounted, config: RefCounted, lock: RefCounted, root_node: Dictionary,
		bid: String, needs_import: bool = true) -> RefCounted:
	lock.doc["generator"] = {"addon_version": Version.VERSION, "installer_version": Installer.INSTALLER_VERSION}
	var lock_bytes: RefCounted = lock.call("to_bytes")
	if not lock_bytes.ok:
		return lock_bytes
	c.add_write(Lock.FILE_NAME, lock_bytes.value)
	var key: String = root_node["ref"].call("key")
	var text: PackedByteArray = wrapper_text(config, root_node, bid)
	var wrapper_rel: String = Wrapper.wrapper_rel(config.call("prefab_rel"), bid)
	c.add_write(wrapper_rel, text)
	c.add_write(State.WRAPPERS_REL, State.wrappers_bytes(c.root, bid, wrapper_rel, Fs.sha256_bytes(text)))
	if needs_import:  # a source binding has nothing to finalize: its wrapper already instances the derived entry scene
		var state: Dictionary = State.read_state(c.root)
		(state["pending_import"] as Array).append(bid)
		c.add_write(State.STATE_REL, State.state_bytes(state))
	var ref: Dictionary = root_node["ref"].call("to_dict")
	if not config.call("has_library", ref["library_id"]):
		config.call("add_library", ref["library_id"], ref["library_id"])
		c.add_write(Config.FILE_NAME, (config.call("to_bytes") as RefCounted).value)
	return Result.success()


## Plain (pre-import) wrapper scene of a resolved root node, as bytes.
static func wrapper_text(config: RefCounted, root_node: Dictionary, bid: String) -> PackedByteArray:
	var key: String = root_node["ref"].call("key")
	var manifest: RefCounted = root_node["prep"]["manifest"]
	var glb: String = "res://" + Installer.target_rel(config.call("managed_rel"), key, manifest.get("raw_sha256")) \
			+ "/" + root_node.get("entry_rel", manifest.data["entrypoint"])
	return Wrapper.scene_text(bid, key, glb, root_node["prep"]["descriptor"].data["placement_anchor"]).to_utf8_buffer()
