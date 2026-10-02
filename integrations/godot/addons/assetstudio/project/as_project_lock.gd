@tool
extends RefCounted
# assetstudio.lock.json: ProjectAssetLockV1 (contracts/godot-integration/v1/project-lock.schema.json).
# Validates every rule of packages/assetstudio_core/project_lock.py and writes canonical bytes identical to the
# Python writer. parse_bytes() rejects non-canonical bytes by default (design §3: readers re-encode and compare).
#
# The model is the plain parsed Dictionary (`doc`); helpers keep its closure invariants.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")
const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")
const CJson = preload("res://addons/assetstudio/core/as_canonical_json.gd")
const AssetRef = preload("res://addons/assetstudio/core/as_asset_ref.gd")

const FILE_NAME: String = "assetstudio.lock.json"
const TOP_KEYS: PackedStringArray = ["schema_version", "generator", "dependencies", "bindings", "roots"]
const DELIVERY_KEYS: PackedStringArray = ["delivery_id", "manifest_sha256", "profile_id", "profile_version"]
const POLICY_MODES: PackedStringArray = ["preserve", "project_mapping", "override"]
const OWNER_KINDS: PackedStringArray = ["scene_binding", "world_generation"]
const MAX_ROOT_KEYS: int = 4096

var doc: Dictionary = {}


static func new_empty(addon_version: String, installer_version: String) -> RefCounted:
	var l: RefCounted = new()
	l.set("doc", {"schema_version": 1, "generator": {"addon_version": addon_version,
			"installer_version": installer_version}, "dependencies": {}, "bindings": {}, "roots": []})
	return l


## ASResult whose value is an ASProjectLock. `require_canonical = false` is for tests and tooling that want
## the semantic verdict on pretty-printed documents.
static func parse_bytes(raw: PackedByteArray, require_canonical: bool = true) -> RefCounted:
	var p: RefCounted = CJson.parse_canonical(raw) if require_canonical else CJson.parse_strict_utf8(raw)
	if not p.ok:
		return Result.fail(Result.CODE_INVALID_PROJECT_FILE, "%s: %s" % [FILE_NAME, p.message])
	var err: String = validate(p.value)
	if err != "":
		return Result.fail(Result.CODE_INVALID_PROJECT_FILE, "%s: %s" % [FILE_NAME, err])
	var l: RefCounted = new()
	l.set("doc", p.value)
	return Result.success(l)


## Canonical bytes (ASResult value); refuses to serialize an invalid document.
func to_bytes() -> RefCounted:
	var err: String = validate(doc)
	if err != "":
		return Result.fail(Result.CODE_INVALID_PROJECT_FILE, "%s: %s" % [FILE_NAME, err])
	return CJson.encode(doc)


# --- validation ----------------------------------------------------------------------------------------------

## Returns "" when valid, else the first violated rule.
static func validate(v: Variant) -> String:
	if not v is Dictionary:
		return "expected an object"
	var d: Dictionary = v
	var err: String = Schema.check_keys(d, TOP_KEYS, PackedStringArray(), "lock")
	if err == "" and Schema.check_int(d["schema_version"], 1, 1, "schema_version") != "":
		err = "schema_version must be 1"
	if err == "":
		err = _check_generator(d["generator"])
	if err == "":
		err = _check_dependencies(d["dependencies"])
	if err == "":
		err = _check_graph(d["dependencies"])
	if err == "":
		err = _check_bindings(d["bindings"], d["dependencies"])
	if err == "":
		err = _check_roots(d["roots"], d["dependencies"])
	return err


static func _check_generator(g: Variant) -> String:
	if not g is Dictionary:
		return "generator: expected object"
	var err: String = Schema.check_keys(g, ["addon_version", "installer_version"], PackedStringArray(), "generator")
	for k: String in ["addon_version", "installer_version"]:
		if err == "":
			err = Schema.check_pattern(g[k], "version_text", "generator.%s" % k)
	return err


static func _check_dependencies(deps: Variant) -> String:
	if not deps is Dictionary:
		return "dependencies: expected object"
	for key: Variant in (deps as Dictionary).keys():
		if not key is String or not Schema.matches("sha256", key):
			return "dependencies: key is not a sha256"
		var err: String = _check_dependency(key, (deps as Dictionary)[key])
		if err != "":
			return err
	return ""


static func _check_dependency(key: String, dep: Variant) -> String:
	if not dep is Dictionary:
		return "dependency %s: expected object" % key
	var d: Dictionary = dep
	var err: String = Schema.check_keys(d, ["asset_ref", "descriptor_sha256", "deliveries", "requires"], PackedStringArray(), "dependency")
	if err == "":
		err = _asset_ref_err(d["asset_ref"])
	if err == "":
		err = Schema.check_pattern(d["descriptor_sha256"], "sha256", "descriptor_sha256")
	if err == "":
		err = _check_deliveries(d["deliveries"])
	if err == "":
		err = _check_requires(d["requires"])
	if err == "":
		var r: Dictionary = d["asset_ref"]
		if Canonical.asset_key(r["server_id"], r["library_id"], r["asset_id"], r["version_id"]) != key:
			err = "dependency key %s does not match its asset_ref" % key
	return err


static func _asset_ref_err(r: Variant) -> String:
	return AssetRef.validate(r, "asset_ref")


static func _check_deliveries(dl: Variant) -> String:
	if not dl is Dictionary or (dl as Dictionary).is_empty():
		return "deliveries: expected non-empty object"
	for rep: Variant in (dl as Dictionary).keys():
		if not rep is String or not Schema.REPRESENTATIONS.has(rep):
			return "deliveries: unknown representation"
		var e: Variant = dl[rep]
		if not e is Dictionary:
			return "deliveries.%s: expected object" % rep
		var err: String = Schema.check_keys(e, DELIVERY_KEYS, PackedStringArray(), "delivery")
		if err == "":
			err = Schema.check_pattern(e["delivery_id"], "delivery_id", "delivery_id")
		if err == "":
			err = Schema.check_pattern(e["manifest_sha256"], "sha256", "manifest_sha256")
		for k: String in ["profile_id", "profile_version"]:
			if err == "":
				err = Schema.check_pattern(e[k], "slug", k)
		if err != "":
			return err
	return ""


static func _check_requires(req: Variant) -> String:
	if not req is Array:
		return "requires: expected array"
	var seen: Dictionary = {}
	for r: Variant in req:
		var err: String = Schema.check_pattern(r, "sha256", "requires")
		if err != "":
			return err
		if seen.has(r):
			return "requires: duplicate entries"
		seen[r] = true
	return ""


static func _check_graph(deps: Dictionary) -> String:
	for key: String in deps:
		for r: String in deps[key]["requires"]:
			if not deps.has(r):
				return "%s requires keys missing from the lock: %s" % [key, r]
	return "" if _is_acyclic(deps) else "dependency cycle"


## Kahn's algorithm (iterative, so deep chains cannot overflow the script stack).
static func _is_acyclic(deps: Dictionary) -> bool:
	var indegree: Dictionary = {}
	var users: Dictionary = {}
	for key: String in deps:
		indegree[key] = (deps[key]["requires"] as Array).size()
		for r: String in deps[key]["requires"]:
			if not users.has(r):
				users[r] = []
			(users[r] as Array).append(key)
	var queue: Array = []
	for key: String in indegree:
		if indegree[key] == 0:
			queue.append(key)
	var done: int = 0
	while not queue.is_empty():
		var k: String = queue.pop_back()
		done += 1
		for u: String in users.get(k, []):
			indegree[u] -= 1
			if indegree[u] == 0:
				queue.append(u)
	return done == deps.size()


static func _check_bindings(bindings: Variant, deps: Dictionary) -> String:
	if not bindings is Dictionary:
		return "bindings: expected object"
	for bid: Variant in (bindings as Dictionary).keys():
		if not bid is String or not Schema.matches("slug", bid):
			return "bindings: key is not a slug"
		var err: String = _check_binding(bid, bindings[bid], deps)
		if err != "":
			return err
	return ""


static func _check_binding(bid: String, b: Variant, deps: Dictionary) -> String:
	if not b is Dictionary:
		return "binding %s: expected object" % bid
	var d: Dictionary = b
	var err: String = Schema.check_keys(d, ["asset_key", "representation", "material_policy", "update_policy"], PackedStringArray(), "binding")
	if err == "":
		err = Schema.check_pattern(d["asset_key"], "sha256", "asset_key")
	if err == "" and (not d["representation"] is String or not Schema.REPRESENTATIONS.has(d["representation"])):
		err = "binding %s: unknown representation" % bid
	if err == "" and d["update_policy"] != "prompt":
		err = "binding %s: update_policy must be 'prompt'" % bid
	if err == "":
		err = _check_policy(d["material_policy"])
	if err == "" and (not deps.has(d["asset_key"]) or not (deps[d["asset_key"]]["deliveries"] as Dictionary).has(d["representation"])):
		err = "binding %s points at a missing dependency or representation" % bid
	return err


static func _check_policy(p: Variant) -> String:
	if not p is Dictionary:
		return "material_policy: expected object"
	var err: String = Schema.check_keys(p, ["mode", "profile_id", "profile_sha256"], PackedStringArray(), "material_policy")
	if err != "":
		return err
	if not p["mode"] is String or not POLICY_MODES.has(p["mode"]):
		return "material_policy.mode: unknown value"
	var has_id: bool = p["profile_id"] != null
	var has_sha: bool = p["profile_sha256"] != null
	if has_id:
		err = Schema.check_pattern(p["profile_id"], "slug", "material_policy.profile_id")
	if err == "" and has_sha:
		err = Schema.check_pattern(p["profile_sha256"], "sha256", "material_policy.profile_sha256")
	if err == "" and has_id != has_sha:
		err = "profile_id and profile_sha256 are both set or both null"
	if err == "" and p["mode"] == "preserve" and has_id:
		err = "preserve takes no profile"
	if err == "" and p["mode"] == "project_mapping" and not has_id:
		err = "project_mapping needs a profile"
	return err


static func _check_roots(roots: Variant, deps: Dictionary) -> String:
	if not roots is Array:
		return "roots: expected array"
	for r: Variant in roots:
		if not r is Dictionary:
			return "root: expected object"
		var err: String = Schema.check_keys(r, ["owner_kind", "owner_id", "asset_keys"], PackedStringArray(), "root")
		if err == "" and (not r["owner_kind"] is String or not OWNER_KINDS.has(r["owner_kind"])):
			err = "root: unknown owner_kind"
		if err == "":
			err = Schema.check_pattern(r["owner_id"], "slug", "root.owner_id")
		if err == "":
			err = _check_root_keys(r, deps)
		if err != "":
			return err
	return ""


static func _check_root_keys(r: Dictionary, deps: Dictionary) -> String:
	var keys: Variant = r["asset_keys"]
	if not keys is Array or (keys as Array).is_empty() or (keys as Array).size() > MAX_ROOT_KEYS:
		return "root %s: asset_keys must have 1..%d items" % [r["owner_id"], MAX_ROOT_KEYS]
	var seen: Dictionary = {}
	for k: Variant in keys:
		var err: String = Schema.check_pattern(k, "sha256", "root.asset_keys")
		if err != "":
			return err
		if seen.has(k):
			return "root %s: duplicate entries" % r["owner_id"]
		if not deps.has(k):
			return "root %s references unknown asset keys" % r["owner_id"]
		seen[k] = true
	return ""


# --- queries -------------------------------------------------------------------------------------------------

func dependencies() -> Dictionary:
	return doc["dependencies"]


func bindings() -> Dictionary:
	return doc["bindings"]


func has_dependency(key: String) -> bool:
	return (doc["dependencies"] as Dictionary).has(key)


## `key` first, then its transitive `requires`, sorted.
func closure(key: String) -> Array:
	var seen: Dictionary = {key: true}
	var queue: Array = [key]
	while not queue.is_empty():
		var k: String = queue.pop_back()
		for r: String in (doc["dependencies"] as Dictionary).get(k, {}).get("requires", []):
			if not seen.has(r):
				seen[r] = true
				queue.append(r)
	var rest: Array = seen.keys()
	rest.erase(key)
	rest.sort()
	return [key] + rest


## Deliveries restore/verify must have on disk: each binding's representation over its closure, plus every
## portable delivery of dependencies no binding reaches. Returns sorted [{"key", "representation"}].
func needed_deliveries() -> Array:
	var need: Dictionary = {}
	var reached: Dictionary = {}
	for bid: String in doc["bindings"]:
		var b: Dictionary = doc["bindings"][bid]
		for k: String in closure(b["asset_key"]):
			reached[k] = true
			var dels: Dictionary = doc["dependencies"][k]["deliveries"]
			var reps: Array = [b["representation"]] if dels.has(b["representation"]) else dels.keys()
			for rep: String in reps:
				need["%s|%s" % [k, rep]] = {"key": k, "representation": rep}
	for k: String in doc["dependencies"]:
		if reached.has(k):
			continue
		for rep: String in doc["dependencies"][k]["deliveries"]:
			if rep == "portable_glb_v1":
				need["%s|%s" % [k, rep]] = {"key": k, "representation": rep}
	var ids: Array = need.keys()
	ids.sort()
	return ids.map(func(i: String) -> Dictionary: return need[i])


# --- mutation helpers ----------------------------------------------------------------------------------------

## Adds or merges one delivery of an exact asset. Returns "" or the reason it conflicts with the lock.
func add_dependency(ref: RefCounted, descriptor_sha256: String, representation: String, delivery: Dictionary,
		requires: Array) -> String:
	var key: String = ref.call("key")
	var deps: Dictionary = doc["dependencies"]
	if not deps.has(key):
		deps[key] = {"asset_ref": ref.call("to_dict"), "descriptor_sha256": descriptor_sha256,
				"deliveries": {}, "requires": []}
	var dep: Dictionary = deps[key]
	if dep["descriptor_sha256"] != descriptor_sha256:
		return "descriptor_sha256 differs from the locked value for %s" % key
	var existing: Variant = (dep["deliveries"] as Dictionary).get(representation)
	if existing != null and existing != delivery:
		return "locked %s delivery for %s differs from the new one" % [representation, key]
	dep["deliveries"][representation] = delivery
	for r: String in requires:
		if not (dep["requires"] as Array).has(r):
			(dep["requires"] as Array).append(r)
	return ""


func add_binding(binding_id: String, key: String, representation: String, policy: Dictionary) -> String:
	if (doc["bindings"] as Dictionary).has(binding_id):
		return "binding %s already exists" % binding_id
	doc["bindings"][binding_id] = {"asset_key": key, "representation": representation,
			"material_policy": policy, "update_policy": "prompt"}
	return ""


func add_root(owner_kind: String, owner_id: String, keys: Array) -> void:
	for r: Dictionary in doc["roots"]:
		if r["owner_kind"] == owner_kind and r["owner_id"] == owner_id:
			r["asset_keys"] = keys
			return
	(doc["roots"] as Array).append({"owner_kind": owner_kind, "owner_id": owner_id, "asset_keys": keys})


## Removes the binding and its scene_binding root, then every dependency no remaining root or binding reaches.
## world_generation roots are never touched.
func remove_binding(binding_id: String) -> void:
	(doc["bindings"] as Dictionary).erase(binding_id)
	doc["roots"] = (doc["roots"] as Array).filter(func(r: Dictionary) -> bool:
		return not (r["owner_kind"] == "scene_binding" and r["owner_id"] == binding_id))
	prune_unreferenced()


func prune_unreferenced() -> void:
	var keep: Dictionary = {}
	for r: Dictionary in doc["roots"]:
		for k: String in r["asset_keys"]:
			for c: String in closure(k):
				keep[c] = true
	for bid: String in doc["bindings"]:
		for c: String in closure(doc["bindings"][bid]["asset_key"]):
			keep[c] = true
	for k: String in (doc["dependencies"] as Dictionary).keys():
		if not keep.has(k):
			(doc["dependencies"] as Dictionary).erase(k)


## `<asset-name-slug>-<first 8 hex of asset_key>`, made unique with -2, -3, ...
func unique_binding_id(name_hint: String, key: String) -> String:
	var base: String = slugify(name_hint)
	var n: int = 1
	while true:
		var tail: String = "-" + key.left(8) + ("" if n == 1 else "-%d" % n)
		var cand: String = base.left(64 - tail.length()) + tail
		if not (doc["bindings"] as Dictionary).has(cand):
			return cand
		n += 1
	return ""


static func slugify(text: String) -> String:
	var out: String = ""
	var last_dash: bool = true
	for i: int in text.length():
		var c: String = text[i].to_lower()
		var ok: bool = (c >= "a" and c <= "z") or (c >= "0" and c <= "9") or (c == "_" or c == "." or c == "-") and not out.is_empty()
		if ok:
			out += c
			last_dash = false
		elif not last_dash:
			out += "-"
			last_dash = true
	out = out.rstrip("-")
	return out if not out.is_empty() else "asset"
