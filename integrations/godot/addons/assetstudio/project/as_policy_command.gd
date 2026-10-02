@tool
extends RefCounted
# `set-policy --binding B (--profile P | --preserve | --override)` (design §7, §9): changes the binding's lock
# material_policy and regenerates its wrapper in ONE transaction. The model must already be imported.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")
const Finalize = preload("res://addons/assetstudio/project/as_finalize.gd")

const ZERO_SHA: String = "0000000000000000000000000000000000000000000000000000000000000000"


static func run(cmd: RefCounted, o: Dictionary) -> int:
	var r: RefCounted = apply_policy(cmd, o)
	if r.ok:
		return 0
	if r.details.get("usage", false):
		cmd.err(r.message)
		return 2
	return cmd.fail_exit(r)


## Not a coroutine: nothing here touches the network. value = {"binding_id", "policy"}.
static func apply_policy(cmd: RefCounted, o: Dictionary) -> RefCounted:
	var policy: RefCounted = policy_from_opts(o)
	if not policy.ok:
		return policy
	var r: RefCounted = Finalize.finalize_bindings(cmd, [o["binding"]], true, policy.value)
	if not r.ok:
		return r
	if not (r.value["failed"] as Array).is_empty():
		var f: Dictionary = r.value["failed"][0]
		return Result.fail(f["code"], f["message"])
	var info: Dictionary = r.value["reports"][o["binding"]]
	cmd.say("%s: material policy %s, %d surface override(s), %d unmapped slot(s)" % [o["binding"],
			policy.value["mode"], info["overrides"], (info["unmapped"] as Array).size()])
	return Result.success({"binding_id": o["binding"], "policy": policy.value})


static func policy_from_opts(o: Dictionary) -> RefCounted:
	if o.has("profile"):
		if not Schema.matches("slug", str(o["profile"])):
			return Result.fail("invalid_request", "profile id must be a slug", false, {"usage": true})
		return Result.success({"mode": "project_mapping", "profile_id": o["profile"], "profile_sha256": ZERO_SHA})
	if o.has("override"):
		return Result.success({"mode": "override", "profile_id": null, "profile_sha256": null})
	return Result.success({"mode": "preserve", "profile_id": null, "profile_sha256": null})
