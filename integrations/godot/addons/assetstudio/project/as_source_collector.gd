@tool
extends RefCounted
# Collects a static scene for publication (design 5.6). The scene is read from its SAVED file on disk (never from
# an open editor tree): its resource closure is walked over the text files (plus ResourceLoader.get_dependencies
# as a cross-check), every dependency is mapped to a package path or to a pinned installed AssetStudio delivery,
# and the scene is instantiated from disk, bypassing the resource cache, into a private holder node for the
# graph analysis (as_source_graph.gd) and, later, the portable export. Nothing in the project or any open scene is
# modified. Anything outside the supported static subset is reported as a problem and blocks publication.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")
const Policy = preload("res://addons/assetstudio/project/as_srcpkg_policy.gd")
const TextScan = preload("res://addons/assetstudio/project/as_source_text.gd")
const Graph = preload("res://addons/assetstudio/project/as_source_graph.gd")
const GlbInspect = preload("res://addons/assetstudio/project/as_glb_inspect.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")

const CAP_ORDER: PackedStringArray = ["godot_text_scene_v1", "csg_static", "static_collision", "shader_source",
		"vertex_colors", "alpha_mask", "alpha_blend", "pbr_textures"]
const UID_PATTERN: String = "^uid://[a-z0-9]{1,32}$"


## Async. opts: managed_root (res://...), lock (ASProjectLock or null), unsaved (PackedStringArray of res paths).
## value (see keys below) carries a live `graph` + `holder`: call release(value) when done.
static func collect(host: Node, root: String, scene_res: String, opts: Dictionary) -> RefCounted:
	var ctx: Dictionary = {"root": root, "problems": [], "warnings": [],
			"files": {}, "refs": {}, "caps": {}, "opts": opts, "queue": [scene_res], "seen": {}}
	_check_entry(scene_res, ctx)
	while not (ctx["queue"] as Array).is_empty() and (ctx["problems"] as Array).size() < 50:
		_visit_file(ctx["queue"].pop_front(), ctx)
	_check_limits(ctx)
	if not (ctx["problems"] as Array).is_empty():
		return _blocked(ctx["problems"])
	var map: Dictionary = _package_paths(ctx, scene_res)
	var graph: RefCounted = await _freeze(host, scene_res, ctx)
	if not graph.ok:
		return graph
	var analysis: Dictionary = graph.value["analysis"]
	if not (analysis["problems"] as Array).is_empty():
		release(graph.value)
		return _blocked(analysis["problems"])
	var collection: Dictionary = _assemble(ctx, map, scene_res, graph.value, analysis)
	if not (ctx["problems"] as Array).is_empty():
		release(collection)
		return _blocked(ctx["problems"])
	return Result.success(collection)


static func release(collection: Dictionary) -> void:
	var holder: Node = collection.get("holder")
	if holder != null and is_instance_valid(holder):
		holder.queue_free()
	collection["holder"] = null
	collection["graph"] = null


static func _blocked(problems: Array) -> RefCounted:
	return Result.fail("unsupported_source", "the scene cannot be published as static source", false,
			{"problems": problems})


# --- closure walk --------------------------------------------------------------------------------------------

static func _check_entry(scene_res: String, ctx: Dictionary) -> void:
	var problems: Array = ctx["problems"]
	if not Fs.is_safe_res_path(scene_res) or not scene_res.to_lower().ends_with(".tscn"):
		problems.append("the scene must be a saved res:// .tscn file (binary .scn is not supported): %s" % scene_res)
	elif not FileAccess.file_exists(Fs.res_to_abs(ctx["root"], scene_res)):
		problems.append("%s does not exist on disk: save the scene first" % scene_res)


static func _visit_file(res: String, ctx: Dictionary) -> void:
	if (ctx["seen"] as Dictionary).has(res):
		return
	ctx["seen"][res] = true
	var ext: String = "." + res.get_extension().to_lower()
	var abs_path: String = Fs.res_to_abs(ctx["root"], res)
	(ctx["files"] as Dictionary)[res] = abs_path
	if (ctx["opts"].get("unsaved", PackedStringArray()) as PackedStringArray).has(res):
		_problem(ctx, "%s has unsaved changes: save it first" % res)
	if Policy.TEXT_EXTENSIONS.has(ext):
		_scan_text(res, abs_path, ext == ".tscn", ctx)
	elif Policy.SHADER_EXTENSIONS.has(ext):
		_scan_shader(res, abs_path, ctx)
	elif ext == ".glb":
		for msg: String in GlbInspect.inspect(Fs.read_bytes(abs_path))["problems"]:
			_problem(ctx, "%s: %s" % [res, msg])


static func _problem(ctx: Dictionary, msg: String) -> void:
	(ctx["problems"] as Array).append(msg)


static func _scan_text(res: String, abs_path: String, is_scene: bool, ctx: Dictionary) -> void:
	var f: FileAccess = FileAccess.open(abs_path, FileAccess.READ)
	if f == null or f.get_length() > TextScan.MAX_BYTES:
		_problem(ctx, "%s cannot be read or is larger than %d bytes" % [res, TextScan.MAX_BYTES])
		return
	var text: String = f.get_as_text()
	f.close()
	var scan: Dictionary = TextScan.scan_text(text)
	for msg: String in TextScan.problems(scan, is_scene, res):
		_problem(ctx, msg)
	_note_node_types(scan, ctx)
	var listed: Dictionary = {}
	for e: Dictionary in TextScan.ext_resources(scan):
		listed[e["path"]] = e["uid"]
		_add_ref(res, e["path"], e["uid"], ctx)
	_add_includes(res, text, ctx)
	_cross_check(res, listed, ctx)


static func _scan_shader(res: String, abs_path: String, ctx: Dictionary) -> void:
	(ctx["caps"] as Dictionary)["shader_source"] = true
	var data: PackedByteArray = Fs.read_bytes(abs_path)
	if data.size() > TextScan.MAX_BYTES:
		_problem(ctx, "%s is larger than %d bytes" % [res, TextScan.MAX_BYTES])
		return
	_add_includes(res, data.get_string_from_utf8(), ctx)


static func _add_includes(res: String, text: String, ctx: Dictionary) -> void:
	var inc: Dictionary = TextScan.includes(text)
	for target: String in inc["paths"]:
		_add_ref(res, target, "", ctx)
	for other: String in inc["bad"]:
		_problem(ctx, "%s: '#include %s' must name a res:// file" % [res, other])


static func _note_node_types(scan: Dictionary, ctx: Dictionary) -> void:
	for s: Dictionary in scan["sections"]:
		var t: String = str((s["attrs"] as Dictionary).get("type", ""))
		if s["kind"] == "node" and t.begins_with("CSG"):
			(ctx["caps"] as Dictionary)["csg_static"] = true
		elif s["kind"] == "node" and t == "CollisionShape3D":
			(ctx["caps"] as Dictionary)["static_collision"] = true
		elif s["kind"] in ["sub_resource", "ext_resource"] and (t == "Shader" or t == "ShaderMaterial"):
			(ctx["caps"] as Dictionary)["shader_source"] = true


## ResourceLoader.get_dependencies() sees references the line scan could miss; anything extra is mapped too.
static func _cross_check(res: String, listed: Dictionary, ctx: Dictionary) -> void:
	for entry: String in ResourceLoader.get_dependencies(res):
		var parts: PackedStringArray = entry.split("::")
		var path: String = parts[parts.size() - 1]
		var uid: String = parts[0] if parts.size() > 1 and parts[0].begins_with("uid://") else ""
		if path.begins_with("res://") and not listed.has(path) and not _same_target(listed, uid):
			_add_ref(res, path, uid, ctx)


static func _same_target(listed: Dictionary, uid: String) -> bool:
	return uid != "" and listed.values().has(uid)


# --- reference resolution ------------------------------------------------------------------------------------

## Records one reference as written (`written`) and queues its real file. `uid` is used only to find the file
## when the written path is stale; it is never trusted for resolution inside the package.
static func _add_ref(from_res: String, written: String, uid: String, ctx: Dictionary) -> void:
	if (ctx["refs"] as Dictionary).has(written):
		return
	if not Fs.is_safe_res_path(written):
		_problem(ctx, "%s: reference '%s' is not a plain res:// path (uid-only, user:// and external references are not supported)" % [from_res, written])
		return
	var actual: String = _resolve(written, uid)
	var original_uid: Variant = uid if RegEx.create_from_string(UID_PATTERN).search(uid) != null else null
	var managed: Dictionary = _managed(actual, ctx)
	if not managed.is_empty():
		ctx["refs"][written] = {"managed": managed, "uid": original_uid, "res": actual}
		return
	var ext: String = "." + actual.get_extension().to_lower()
	if not Policy.ALLOWED_EXTENSIONS.has(ext):
		_problem(ctx, "%s: '%s' is not a supported file type (%s)" % [from_res, actual, ext])
	elif not FileAccess.file_exists(Fs.res_to_abs(ctx["root"], actual)):
		_problem(ctx, "%s: dependency '%s' does not exist on disk" % [from_res, actual])
	else:
		ctx["refs"][written] = {"res": actual, "uid": original_uid}
		(ctx["queue"] as Array).append(actual)


static func _resolve(written: String, uid: String) -> String:
	if uid != "":
		var id: int = ResourceUID.text_to_id(uid)
		if id != ResourceUID.INVALID_ID and ResourceUID.has_id(id):
			var by_uid: String = ResourceUID.get_id_path(id)
			if by_uid.begins_with("res://"):
				return by_uid
	return written


## {"key", "msha", "entrypoint"} when `res` lies inside an installed delivery <managed_root>/<key>/<sha>/...
static func _managed(res: String, ctx: Dictionary) -> Dictionary:
	var prefix: String = str(ctx["opts"].get("managed_root", "res://assets/library")).trim_suffix("/") + "/"
	if not res.begins_with(prefix):
		return {}
	var rest: PackedStringArray = res.substr(prefix.length()).split("/")
	if rest.size() < 3 or not Schema.matches("sha256", rest[0]) or not Schema.matches("sha256", rest[1]):
		return {}
	return {"key": rest[0], "msha": rest[1], "entrypoint": "/".join(rest.slice(2))}


static func _check_limits(ctx: Dictionary) -> void:
	var total: int = 0
	for res: String in ctx["files"]:
		var f: FileAccess = FileAccess.open(ctx["files"][res], FileAccess.READ)
		total += f.get_length() if f != null else 0
	if (ctx["files"] as Dictionary).size() >= Policy.MAX_FILES:
		_problem(ctx, "more than %d files in the closure" % Policy.MAX_FILES)
	if total > Policy.MAX_EXPANDED_BYTES:
		_problem(ctx, "the closure expands to more than %d bytes" % Policy.MAX_EXPANDED_BYTES)


# --- package paths -------------------------------------------------------------------------------------------

## res path -> package path for every package file (deterministic: sorted input, case-fold unique).
static func _package_paths(ctx: Dictionary, scene_res: String) -> Dictionary:
	var out: Dictionary = {}
	var taken: Dictionary = {}
	var needed: Dictionary = {scene_res: true}
	for r: Dictionary in (ctx["refs"] as Dictionary).values():
		if not r.has("managed"):
			needed[r["res"]] = true
	var all: Array = needed.keys()
	all.sort()
	for res: String in all:
		var path: String = _unique_path(_safe_package_path(res), taken)
		taken[path.to_lower()] = true
		out[res] = path
		if path.length() > 255 or path.count("/") >= Policy.MAX_DEPTH:
			_problem(ctx, "%s: package path is too long or too deep" % res)
	return out


static func _safe_package_path(res: String) -> String:
	var segments := PackedStringArray()
	for seg: String in res.trim_prefix("res://").split("/"):
		var clean: String = ""
		for ch: String in seg:
			clean += ch if (ch >= "a" and ch <= "z") or (ch >= "A" and ch <= "Z") or (ch >= "0" and ch <= "9") or ch in ["_", ".", "-"] else "_"
		segments.append("_" if clean == "." or clean == ".." or clean == "" else clean)
	var path: String = "/".join(segments)
	var ext: String = path.get_extension()
	return path.get_basename() + "." + ext.to_lower() if ext != "" else path


static func _unique_path(path: String, taken: Dictionary) -> String:
	var candidate: String = path
	var n: int = 2
	while taken.has(candidate.to_lower()):
		candidate = "%s_%d.%s" % [path.get_basename(), n, path.get_extension()]
		n += 1
	return candidate


# --- frozen graph --------------------------------------------------------------------------------------------

static func _freeze(host: Node, scene_res: String, ctx: Dictionary) -> RefCounted:
	var packed: PackedScene = ResourceLoader.load(scene_res, "PackedScene", ResourceLoader.CACHE_MODE_IGNORE_DEEP) as PackedScene
	if packed == null:
		return Result.fail("unsupported_source", "cannot load %s (run the headless import first: godot --headless --editor --path <project> --import)" % scene_res)
	var graph: Node = packed.instantiate(PackedScene.GEN_EDIT_STATE_DISABLED)
	if graph == null:
		return Result.fail("unsupported_source", "cannot instantiate %s" % scene_res)
	var holder := Node.new()
	holder.name = "AssetStudioPublishHolder"
	host.add_child(holder)
	holder.add_child(graph)
	await host.get_tree().process_frame
	await host.get_tree().process_frame
	return Result.success({"holder": holder, "graph": graph, "analysis": Graph.analyze(graph)})


# --- result --------------------------------------------------------------------------------------------------

static func _assemble(ctx: Dictionary, map: Dictionary, scene_res: String, frozen: Dictionary, analysis: Dictionary) -> Dictionary:
	var resource_map: Dictionary = {}
	var deps: Dictionary = {}
	for written: String in ctx["refs"]:
		var r: Dictionary = ctx["refs"][written]
		if r.has("managed"):
			var d: RefCounted = _dependency_entry(r["managed"], ctx)
			if d.ok:
				resource_map[written] = {"kind": "asset_dependency", "asset_key": r["managed"]["key"],
						"entrypoint": r["managed"]["entrypoint"], "original_uid": r["uid"]}
				deps[r["managed"]["key"]] = d.value
			else:
				_problem(ctx, "%s: %s" % [written, d.message])
		else:
			resource_map[written] = {"kind": "package_file", "path": map[r["res"]], "original_uid": r["uid"]}
	var files: Array = []
	for res: String in map:
		files.append({"path": map[res], "res": res, "abs": ctx["files"][res],
				"media_type": Policy.MEDIA["." + res.get_extension().to_lower()]})
	files.sort_custom(func(a: Dictionary, b: Dictionary) -> bool: return String(a["path"]) < String(b["path"]))
	var caps: Dictionary = (ctx["caps"] as Dictionary).duplicate()
	caps.merge(analysis["caps"])
	var warnings: Array = ctx["warnings"]
	if caps.has("shader_source"):
		warnings.append("the package contains shader source: desktop trust is required and shaders are not executed on iPad")
	return {"scene_res": scene_res, "entry_scene": map[scene_res], "files": files, "resource_map": resource_map,
			"asset_dependencies": deps, "capabilities": _caps_list(caps), "slots": analysis["slots"],
			"placement": Graph.placement_values(analysis), "collision": _collision(analysis),
			"baked": analysis["baked"], "graph": frozen["graph"], "holder": frozen["holder"],
			"warnings": warnings, "problems": ctx["problems"]}


static func _caps_list(caps: Dictionary) -> Array:
	var out: Array = ["godot_text_scene_v1"]
	for c: String in CAP_ORDER:
		if caps.has(c) and not out.has(c):
			out.append(c)
	return out


static func _collision(analysis: Dictionary) -> Variant:
	var shapes: Array = analysis["shapes"]
	if shapes.is_empty():
		return null
	var kinds: Array = []
	for k: String in shapes:
		if not kinds.has(k):
			kinds.append(k)
	kinds.sort()
	return {"source": "godot_static_source_v1", "shape_count": shapes.size(), "shape_types": kinds}


## value = {"asset_ref", "descriptor_sha256", "representation", "delivery_id"} from the project lock.
static func _dependency_entry(m: Dictionary, ctx: Dictionary) -> RefCounted:
	var lock: RefCounted = ctx["opts"].get("lock")
	if lock == null or not (lock.call("dependencies") as Dictionary).has(m["key"]):
		return Result.fail("unsupported_source_dependency", "it lies in an installed delivery that is not in assetstudio.lock.json")
	var dep: Dictionary = (lock.call("dependencies") as Dictionary)[m["key"]]
	for rep: String in dep["deliveries"]:
		if dep["deliveries"][rep]["manifest_sha256"] == m["msha"] and ["portable_glb_v1", "godot_static_source_v1"].has(rep):
			return Result.success({"asset_ref": dep["asset_ref"], "descriptor_sha256": dep["descriptor_sha256"],
					"representation": rep, "delivery_id": dep["deliveries"][rep]["delivery_id"]})
	return Result.fail("unsupported_source_dependency", "no locked delivery matches the installed manifest hash")
