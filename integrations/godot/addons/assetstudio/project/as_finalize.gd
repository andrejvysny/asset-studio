@tool
extends RefCounted
# `finalize` (design §9): after the headless import, resolve the descriptor slots on the imported scene, apply the
# binding's material policy and rewrite the wrapper scene. Used by the CLI and by the dock (same code path).
#
# prepare() is the pure builder (no writes): it verifies the installed delivery, loads the imported model, applies
# the policy and returns the wrapper bytes. finalize_bindings() queues the writes on a coordinator transaction.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Descriptor = preload("res://addons/assetstudio/core/as_asset_descriptor.gd")
const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")
const Lock = preload("res://addons/assetstudio/project/as_project_lock.gd")
const Installer = preload("res://addons/assetstudio/project/as_installer.gd")
const Wrapper = preload("res://addons/assetstudio/project/as_wrapper.gd")
const State = preload("res://addons/assetstudio/project/as_project_state.gd")
const SlotResolver = preload("res://addons/assetstudio/project/as_slot_resolver.gd")
const MaterialPolicy = preload("res://addons/assetstudio/project/as_material_policy.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")


## Installed directory (relative to the project) of a binding's delivery.
static func delivery_rel(config: RefCounted, lock: RefCounted, binding_id: String) -> String:
	var b: Dictionary = lock.bindings()[binding_id]
	var locked: Dictionary = lock.dependencies()[b["asset_key"]]["deliveries"][b["representation"]]
	return Installer.target_rel(config.call("managed_rel"), b["asset_key"], locked["manifest_sha256"])


## value = parsed ASAssetDescriptor of a locked dependency, read from the blob cache and re-verified.
static func load_descriptor(cache: RefCounted, dep: Dictionary) -> RefCounted:
	var raw: PackedByteArray = cache.load_document("descriptors", dep["descriptor_sha256"])
	if raw.is_empty() or Canonical.sha256_hex(raw) != dep["descriptor_sha256"]:
		return Result.fail("temporarily_unavailable", "descriptor %s is not in the local cache (run restore)" % str(dep["descriptor_sha256"]).left(12))
	return Descriptor.parse_bytes(raw)


## Builds the wrapper for `binding_id` with `policy` ({mode, profile_id, profile_sha256}). value = {"bytes",
## "rel", "policy" (profile_sha256 refreshed when `reapply`), "info" {overrides, unmapped, unresolved}}.
## Fails with "profile_changed" when a project_mapping profile no longer matches the locked sha256 and
## `reapply` is false, with "conflict" when the existing wrapper was modified by hand.
static func prepare(root: String, config: RefCounted, lock: RefCounted, cache: RefCounted, binding_id: String,
		policy: Dictionary, reapply: bool) -> RefCounted:
	var b: Dictionary = lock.bindings()[binding_id]
	var dep: Dictionary = lock.dependencies()[b["asset_key"]]
	var rel: String = Wrapper.wrapper_rel(config.call("prefab_rel"), binding_id)
	var conflict: String = Wrapper.check_conflict(root, binding_id, rel)
	if conflict != "":
		return Result.fail("conflict", conflict)
	var drel: String = delivery_rel(config, lock, binding_id)
	var dir: String = root.path_join(drel)
	var locked: Dictionary = dep["deliveries"][b["representation"]]
	var problems: PackedStringArray = Installer.check_install(dir, {"asset_key": b["asset_key"],
			"manifest_sha256": locked["manifest_sha256"], "delivery_id": locked["delivery_id"],
			"representation": b["representation"]})
	if not problems.is_empty():
		return Result.fail("integrity_mismatch", "installed delivery is not intact: %s" % problems[0])
	var desc: RefCounted = load_descriptor(cache, dep)
	if not desc.ok:
		return desc
	var pol: RefCounted = _load_policy(root, config, binding_id, policy, reapply)
	if not pol.ok:
		return pol
	return _build(root, binding_id, b, drel, desc.value, pol.value, rel)


## value = {"policy": Dictionary, "rules": Array or null (preserve)}.
static func _load_policy(root: String, config: RefCounted, binding_id: String, policy: Dictionary,
		reapply: bool) -> RefCounted:
	var out: Dictionary = policy.duplicate()
	if policy["mode"] == "preserve":
		return Result.success({"policy": out, "rules": null})
	var pid: String = binding_id if policy["mode"] == "override" else str(policy["profile_id"])
	var file: String = MaterialPolicy.profile_file(root, config, policy["mode"], pid, binding_id)
	if not FileAccess.file_exists(file):
		return Result.fail(Result.CODE_INVALID_PROJECT_FILE, "material profile file %s not found" % file.trim_prefix(
				Fs.res_to_abs(root, config.get("material_profiles_dir")) + "/"))
	var parsed: RefCounted = MaterialPolicy.parse(Fs.read_bytes(file), pid)
	if not parsed.ok:
		return parsed
	if policy["mode"] == "project_mapping" and parsed.value["sha256"] != policy["profile_sha256"]:
		if not reapply:
			return Result.fail("profile_changed", "profile %s changed since it was locked (re-apply to accept)" % pid)
		out["profile_sha256"] = parsed.value["sha256"]
	return Result.success({"policy": out, "rules": parsed.value["rules"]})


static func _build(root: String, binding_id: String, b: Dictionary, drel: String, desc: RefCounted,
		pol: Dictionary, rel: String) -> RefCounted:
	var dir: String = root.path_join(drel)
	var receipt: Array = _glb_names(dir)
	if receipt.is_empty():
		return Result.fail("unsupported_representation", "delivery has no .glb file")
	var glb_res: String = "res://" + drel + "/" + receipt[0]
	var scene: PackedScene = ResourceLoader.load(glb_res) as PackedScene if ResourceLoader.exists(glb_res) else null
	if scene == null:
		return Result.fail("temporarily_unavailable", "%s is not imported yet (run the headless import)" % glb_res)
	var glb: PackedByteArray = Fs.read_bytes(dir.path_join(receipt[0]))
	var customize: Callable = _customize.bind(glb, desc.data["material_slots"], pol["rules"])
	var built: RefCounted = Wrapper.build_bytes(binding_id, b["asset_key"], scene, desc.data["placement_anchor"], customize)
	if not built.ok:
		return built
	return Result.success({"bytes": built.value["bytes"], "rel": rel, "policy": pol["policy"],
			"info": built.value["info"]})


static func _glb_names(dir: String) -> Array:
	var names: Array = []
	for f: String in Fs.list_dir(dir)["files"]:
		if f.get_extension().to_lower() == "glb":
			names.append(f)
	return names


static func _customize(model: Node3D, glb: PackedByteArray, slots: Array, rules: Variant) -> RefCounted:
	var info: Dictionary = {"overrides": 0, "unmapped": [], "unresolved": []}
	if rules == null:
		return Result.success(info)
	var meshes: RefCounted = SlotResolver.gltf_meshes(glb)
	if not meshes.ok:
		return meshes
	var resolved: RefCounted = SlotResolver.resolve(meshes.value, model, slots)
	var applied: RefCounted = MaterialPolicy.apply(model, rules, slots, resolved.value["slots"])
	if not applied.ok:
		return applied
	info["overrides"] = applied.value["overrides"]
	info["unmapped"] = applied.value["unmapped"]
	info["unresolved"] = resolved.value["unresolved"]
	return Result.success(info)


## Rewrites the wrapper of each id in one transaction. `cmd` = ASCommands (root, cache, open_txn).
## value = {"done": [id], "failed": [{"id", "code", "message"}], "reports": {id: info}}. Failed bindings stay
## pending. Nothing is written when nothing succeeded. `policy_override` replaces the locked material policy of
## the (single) binding, which is how set-policy changes policy and wrapper in one transaction.
static func finalize_bindings(cmd: RefCounted, ids: Array, reapply: bool, policy_override: Dictionary = {}) -> RefCounted:
	var c: RefCounted = cmd.open_txn("finalize")
	if c == null:
		return Result.fail(Result.CODE_LOCKED, "cannot open a transaction")
	var loaded: RefCounted = cmd.load_locked()
	if not loaded.ok:
		c.close()
		return loaded
	var lock: RefCounted = loaded.value["lock"]
	var state: Dictionary = State.read_state(cmd.root)
	var targets: Array = ids if not ids.is_empty() else state["pending_import"]
	var out: Dictionary = {"done": [], "failed": [], "reports": {}}
	var wrote: Dictionary = {}
	var lock_changed: bool = false
	for id: String in targets:
		var r: RefCounted = _one(cmd, loaded.value["config"], lock, id, reapply, policy_override)
		if not r.ok:
			out["failed"].append({"id": id, "code": r.code, "message": r.message})
			continue
		var v: Dictionary = r.value
		c.add_write(v["rel"], v["bytes"])
		wrote[id] = {"path": v["rel"], "sha256": Fs.sha256_bytes(v["bytes"])}
		if v["policy"] != lock.bindings()[id]["material_policy"]:
			lock.bindings()[id]["material_policy"] = v["policy"]
			lock_changed = true
		out["done"].append(id)
		out["reports"][id] = v["info"]
	if wrote.is_empty():
		c.close()
		return Result.success(out)
	var queued: RefCounted = _queue_state(c, cmd.root, state, lock, out["done"], wrote, lock_changed)
	if queued.ok:
		c.summary = {"kind": "finalize", "bindings": out["done"]}
		queued = c.commit()
	c.close()
	return queued if not queued.ok else Result.success(out)


static func _one(cmd: RefCounted, config: RefCounted, lock: RefCounted, id: String, reapply: bool,
		policy_override: Dictionary) -> RefCounted:
	if not lock.bindings().has(id):
		return Result.fail("invalid_request", "unknown binding %s" % id)
	var policy: Dictionary = policy_override if not policy_override.is_empty() else lock.bindings()[id]["material_policy"]
	return prepare(cmd.root, config, lock, cmd.cache, id, policy, reapply)


static func _queue_state(c: RefCounted, root: String, state: Dictionary, lock: RefCounted, done: Array,
		wrote: Dictionary, lock_changed: bool) -> RefCounted:
	c.add_write(State.WRAPPERS_REL, State.wrappers_bytes_multi(root, wrote))
	var pending: Array = (state["pending_import"] as Array).filter(func(i: Variant) -> bool: return not done.has(i))
	if pending.size() != (state["pending_import"] as Array).size():
		state["pending_import"] = pending
		c.add_write(State.STATE_REL, State.state_bytes(state))
	if lock_changed:
		var bytes: RefCounted = lock.to_bytes()
		if not bytes.ok:
			return bytes
		c.add_write(Lock.FILE_NAME, bytes.value)
	return Result.success()


## CLI entry: `finalize [--binding id] [--reapply]`.
static func run(cmd: RefCounted, opts: Dictionary) -> int:
	var ids: Array = [opts["binding"]] if opts.has("binding") else []
	var r: RefCounted = finalize_bindings(cmd, ids, opts.has("reapply"))
	if not r.ok:
		return cmd.fail_exit(r)
	for id: String in r.value["done"]:
		var info: Dictionary = r.value["reports"][id]
		cmd.say("finalized %s: %d surface override(s), %d unmapped slot(s), %d unresolved surface(s)" % [id,
				info["overrides"], (info["unmapped"] as Array).size(), (info["unresolved"] as Array).size()])
		for u: Variant in info["unmapped"]:
			cmd.say("  unmapped slot (source material kept): %s" % str(u))
		for u: Dictionary in info["unresolved"]:
			cmd.say("  unresolved surface: %s (%s)" % [u["slot_id"], u["reason"]])
	for f: Dictionary in r.value["failed"]:
		cmd.err("%s: %s: %s" % [f["id"], f["code"], f["message"]])
	if r.value["done"].is_empty() and r.value["failed"].is_empty():
		cmd.say("nothing to finalize")
	return 1 if not (r.value["failed"] as Array).is_empty() else 0
