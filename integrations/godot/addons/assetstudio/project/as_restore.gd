@tool
extends RefCounted
# `restore --locked` and `verify --locked --offline` (design §4.1, §4.2).
#
# restore: the lock is the INPUT and is never rewritten. Each needed delivery is prepared through the resolver with
# the exact locked delivery_id pinned, then checked against the locked manifest and descriptor hashes; any
# difference is integrity_mismatch (a newer profile or another delivery is never substituted). Missing deliveries
# are installed through the caller's coordinator transaction; intact ones are left alone; modified ones are
# reported and never overwritten.
#
# verify: pure file inspection. It takes no resolver, client or cache, so it cannot touch the network.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const AssetRef = preload("res://addons/assetstudio/core/as_asset_ref.gd")
const Installer = preload("res://addons/assetstudio/project/as_installer.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")

const SUPPORTED_REPRESENTATION: String = "portable_glb_v1"


## `coord` must be open. value = {"installed": int, "present": int}. The caller commits (or closes) `coord`.
## `resolver.offline_only = true` makes this fully offline.
static func restore(resolver: Node, lock: RefCounted, config: RefCounted, coord: RefCounted) -> RefCounted:
	var installed: int = 0
	var present: int = 0
	for need: Dictionary in lock.call("needed_deliveries"):
		var dep: Dictionary = (lock.call("dependencies") as Dictionary)[need["key"]]
		var rep: String = need["representation"]
		if rep != SUPPORTED_REPRESENTATION:
			return Result.fail("unsupported_representation", "%s deliveries need AS-07b" % rep)
		var prep: RefCounted = await fetch_pinned(resolver, dep, rep)
		if not prep.ok:
			return prep
		var ref: RefCounted = AssetRef.parse(dep["asset_ref"]).value
		var inst: RefCounted = Installer.install(coord, config.call("managed_rel"), ref, prep.value)
		if not inst.ok:
			return inst
		if inst.value["status"] == "installed":
			installed += 1
		else:
			present += 1
	return Result.success({"installed": installed, "present": present})


## Prepares the locked delivery and enforces the exact pins. value = resolver.prepare() value.
static func fetch_pinned(resolver: Node, dep: Dictionary, representation: String) -> RefCounted:
	var locked: Dictionary = dep["deliveries"][representation]
	var ref: RefCounted = AssetRef.parse(dep["asset_ref"]).value
	var r: RefCounted = await resolver.prepare(ref, representation, null, locked["delivery_id"])
	if not r.ok:
		return r
	var v: Dictionary = r.value
	if v["delivery_id"] != locked["delivery_id"] or v["manifest"].get("raw_sha256") != locked["manifest_sha256"]:
		return Result.fail("integrity_mismatch", "delivery does not match the locked delivery_id/manifest sha256")
	if v["descriptor"].get("raw_sha256") != dep["descriptor_sha256"]:
		return Result.fail("integrity_mismatch", "descriptor does not match the locked descriptor_sha256")
	return r


## Offline check of every needed delivery. Fails with details.problems listing everything wrong.
static func verify_locked(root: String, lock: RefCounted, config: RefCounted) -> RefCounted:
	var problems := PackedStringArray()
	var checked: int = 0
	for need: Dictionary in lock.call("needed_deliveries"):
		var key: String = need["key"]
		var locked: Dictionary = (lock.call("dependencies") as Dictionary)[key]["deliveries"][need["representation"]]
		var rel: String = Installer.target_rel(config.call("managed_rel"), key, locked["manifest_sha256"])
		var dir: String = root.path_join(rel)
		var label: String = "%s/%s" % [key.left(12), need["representation"]]
		checked += 1
		if not DirAccess.dir_exists_absolute(dir):
			problems.append("%s: not installed (%s)" % [label, rel])
			continue
		var expect: Dictionary = {"asset_key": key, "manifest_sha256": locked["manifest_sha256"],
				"delivery_id": locked["delivery_id"], "representation": need["representation"]}
		for p: String in Installer.check_install(dir, expect):
			problems.append("%s: %s" % [label, p])
		for f: String in Installer.missing_import_files(dir):
			problems.append("%s: %s has no .import file (not imported)" % [label, f])
	if not problems.is_empty():
		return Result.fail("integrity_mismatch", "%d problem(s): %s" % [problems.size(), problems[0]], false,
				{"problems": Array(problems)})
	return Result.success({"checked": checked})
