@tool
extends RefCounted
# Content rules of a source package (static-source-package.md §3-§4): text resources, shaders, reference closure
# and capabilities. Rule order mirrors the server validator; the first violation is returned. Pure data checks,
# nothing is loaded or executed.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Zip = preload("res://addons/assetstudio/project/as_srcpkg_zip.gd")
const Policy = preload("res://addons/assetstudio/project/as_srcpkg_policy.gd")
const GText = preload("res://addons/assetstudio/project/as_godot_text.gd")

var manifest: Dictionary
var files: Dictionary = {}
var detected: Dictionary = {"godot_text_scene_v1": true}
var edges: Dictionary = {}
var deps_of: Dictionary = {}
var instances: Dictionary = {}
var includes: Dictionary = {}


func _init(m: Dictionary) -> void:
	manifest = m
	for f: Dictionary in m["files"]:
		files[f["path"]] = true


static func err(detail: String, message: String, path: String, line: int = 0, code: String = "unsafe_package") -> RefCounted:
	return Result.fail(code, message if line == 0 else "%s (line %d)" % [message, line], false,
			{"detail": detail, "path": path, "line": line})


## .tscn / .tres bytes. The document is parsed, then every rule is applied in server order.
func check_text(path: String, data: PackedByteArray, suffix: String) -> RefCounted:
	var parsed: RefCounted = GText.parse_bytes(data)
	if not parsed.ok:
		return err(parsed.details["detail"], parsed.message, path, 0, parsed.code)
	var doc: Dictionary = parsed.value
	var expected: String = "gd_scene" if suffix == ".tscn" else "gd_resource"
	if doc["kind"] != expected:
		return err("wrong_header", "%s must start with [%s]" % [suffix, expected], path)
	var t := TextRules.new(self, path, doc)
	var r: RefCounted = t.run()
	if r.ok:
		edges[path] = (edges.get(path, []) as Array) + t.paths
		deps_of[path] = (deps_of.get(path, []) as Array) + t.deps
		if suffix == ".tscn":
			instances[path] = t.instance_paths
	return r


func check_shader(path: String, data: PackedByteArray) -> RefCounted:
	var text: String = data.get_string_from_utf8()
	if text.to_utf8_buffer() != data:
		return err("not_utf8", "shader is not valid UTF-8", path)
	detected["shader_source"] = true
	return check_includes(path, text, 0)


## Every `#include` must be a "res://" package_file reference or a relative path to a package shader.
func check_includes(path: String, text: String, base_line: int) -> RefCounted:
	var n: int = 0
	for line: String in text.split("\n"):
		n += 1
		var inc: Dictionary = GText.parse_include(line)
		if inc.is_empty():
			continue
		var target: String = resolve_include(path, inc["arg"]) if inc["ok"] else ""
		if target.is_empty():
			return err("shader_include_escape", "#include leaves the package or is unmapped", path, base_line + n)
		edges[path] = (edges.get(path, []) as Array) + [target]
		includes[path] = (includes.get(path, []) as Array) + [target]
	return Result.success()


## Package path of an include target, or "".
func resolve_include(from_path: String, target: String) -> String:
	if target.begins_with("res://"):
		var entry: Variant = (manifest["resource_map"] as Dictionary).get(target)
		return entry["path"] if entry != null and entry["kind"] == "package_file" else ""
	if target.contains("://") or target.contains("\\") or target.begins_with("/") or target.is_empty():
		return ""
	var joined: String = from_path.get_base_dir().path_join(target).simplify_path()
	var ok_ext: bool = joined.ends_with(".gdshader") or joined.ends_with(".gdshaderinc")
	return joined if files.has(joined) and ok_ext else ""


## Reachability from the entry scene, capabilities and cycles (run after every member was checked).
func finish() -> RefCounted:
	var reached: Dictionary = {}
	var queue: Array = [manifest["entry_scene"]]
	while not queue.is_empty():
		var p: String = queue.pop_back()
		if not reached.has(p):
			reached[p] = true
			queue.append_array(edges.get(p, []))
	for path: String in _sorted(files.keys()):
		if not reached.has(path):
			return err("unreferenced_file", "declared file is not reachable from entry_scene", path)
	var used: Dictionary = {}
	for p: String in reached:
		for k: String in deps_of.get(p, []):
			used[k] = true
	for cap: String in ["csg_static", "static_collision", "shader_source"]:
		if detected.has(cap) and not (manifest["capabilities"] as Array).has(cap):
			return err("capability_undeclared", "package uses %s but the manifest does not declare it" % cap, "")
	for pair: Array in [["instance_cycle", instances], ["include_cycle", includes]]:
		var cycle: Array = _find_cycle(pair[1])
		if not cycle.is_empty():
			return err(pair[0], "cycle: " + " -> ".join(PackedStringArray(cycle)), cycle[0])
	return Result.success({"dependencies": _sorted(used.keys()), "shader_source": detected.has("shader_source"),
			"detected": _sorted(detected.keys())})


static func _sorted(a: Array) -> Array:
	var out: Array = a.duplicate()
	out.sort()
	return out


## One cycle (list of paths ending at its start) or [].
static func _find_cycle(graph: Dictionary) -> Array:
	var state: Dictionary = {}
	for start: String in _sorted(graph.keys()):
		if state.has(start):
			continue
		var trail: Array = [start]
		var stack: Array = [[start, 0]]
		state[start] = 1
		while not stack.is_empty():
			var top: Array = stack.back()
			var kids: Array = _sorted(graph.get(top[0], []))
			if top[1] >= kids.size():
				state[top[0]] = 2
				stack.pop_back()
				trail.pop_back()
				continue
			var nxt: String = kids[top[1]]
			top[1] += 1
			if state.get(nxt, 0) == 1:
				return trail.slice(trail.find(nxt)) + [nxt]
			if not state.has(nxt):
				state[nxt] = 1
				trail.append(nxt)
				stack.append([nxt, 0])
	return []


## Rules for one parsed .tscn/.tres.
class TextRules:
	var ck  # the checker (outer script): untyped so its members resolve dynamically
	var path: String
	var doc: Dictionary
	var ext: Dictionary = {}
	var sub_ids: Dictionary = {}
	var target: Dictionary = {}
	var paths: Array = []
	var deps: Array = []
	var instance_paths: Array = []
	var _sections: Array = []

	func _init(checker: Object, p: String, d: Dictionary) -> void:
		ck = checker
		path = p
		doc = d
		var current: Dictionary = {}
		for i: int in (doc["statements"] as Array).size():
			var s: Dictionary = doc["statements"][i]
			if s["kind"] == "header":
				current = s
				s["props"] = []
				if i != int(doc["root_index"]):
					_sections.append(s)
			elif s["kind"] == "prop" and not current.is_empty():
				(current["props"] as Array).append(s)

	func fail(detail: String, message: String, line: int = 0, code: String = "unsafe_package") -> RefCounted:
		return ck.err(detail, message, path, line, code)

	func run() -> RefCounted:
		var steps: Array[Callable] = [_index, _section_kinds, _scripts, _types, _refs, _constructors, _strings,
				_ext_paths, _nodes, _inline_shaders]
		for step: Callable in steps:
			var r: RefCounted = step.call()
			if not r.ok:
				return r
		return Result.success()

	func sections() -> Array:
		return _sections

	func props_of(header: Dictionary) -> Array:
		return header["props"]

	func _index() -> RefCounted:
		for sec: Dictionary in sections():
			if sec["section"] != "ext_resource" and sec["section"] != "sub_resource":
				continue
			var rid: String = GText.str_attr(sec, "id")
			var seen: Dictionary = ext if sec["section"] == "ext_resource" else sub_ids
			if rid.is_empty() or seen.has(rid):
				return fail("malformed_section", "%s needs a unique string id" % sec["section"], sec["line"])
			seen[rid] = sec
		return Result.success()

	func _section_kinds() -> RefCounted:
		for sec: Dictionary in sections():
			if sec["section"] == "connection":
				return fail("connection", "signal connections are not allowed", sec["line"])
			if not Policy.SECTION_KINDS.has(sec["section"]):
				return fail("section_not_allowed", "section [%s] is not allowed" % sec["section"], sec["line"])
		return Result.success()

	func _scripts() -> RefCounted:
		var root: Dictionary = doc["statements"][doc["root_index"]]
		if GText.attr(root, "script_class") != null:
			return fail("script_property", "header declares script_class", 1)
		for sec: Dictionary in sections():
			if GText.attr(sec, "script_class") != null:
				return fail("script_property", "section declares script_class", sec["line"])
			for prop: Dictionary in props_of(sec):
				if prop["key"] == "script" or prop["key"].begins_with("metadata/_custom_type_script"):
					return fail("script_property", "property '%s' attaches a script" % prop["key"], sec["line"])
			if _refs_script(sec):
				return fail("script_property", "value references a Script resource", sec["line"])
		return Result.success()

	func _section_values(sec: Dictionary) -> Array:
		var out: Array = []
		for a: Dictionary in sec["attrs"]:
			out.append(a["node"])
		for prop: Dictionary in props_of(sec):
			out.append(prop["value"])
		return out

	func _refs_script(sec: Dictionary) -> bool:
		var refs: Array = []
		for v: Dictionary in _section_values(sec):
			GText.collect(v, refs, [], [])
		for r: Dictionary in refs:
			var t: Variant = ext.get(r["id"]) if r["kind"] == "ExtResource" else null
			if t != null and Policy.SCRIPT_TYPES.has(GText.str_attr(t, "type")):
				return true
		return false

	func _type_ok(value: String, allowed: PackedStringArray) -> bool:
		return not value.is_empty() and not Policy.SCRIPT_TYPES.has(value) and allowed.has(value)

	func _types() -> RefCounted:
		for sec: Dictionary in sections():
			var kind: String = sec["section"]
			var t: String = GText.str_attr(sec, "type")
			if kind == "ext_resource" or kind == "sub_resource":
				if not _type_ok(t, Policy.ALLOWED_RESOURCE_TYPES) and not (kind == "ext_resource" and Policy.EXT_SAVE_CLASSES.has(t)):
					return fail("type_not_allowed", "%s type '%s' is not in the allowlist" % [kind, t], sec["line"])
			elif kind == "node":
				if t.is_empty() and GText.attr(sec, "instance") != null:
					continue
				if t.is_empty():
					return fail("node_missing_type", "node has neither type nor instance", sec["line"])
				if not _type_ok(t, Policy.ALLOWED_NODE_TYPES):
					return fail("type_not_allowed", "node type '%s' is not in the allowlist" % t, sec["line"])
		if doc["kind"] == "gd_resource":
			var rt: String = GText.str_attr(doc["statements"][doc["root_index"]], "type")
			if not _type_ok(rt, Policy.ALLOWED_RESOURCE_TYPES):
				return fail("type_not_allowed", "gd_resource type '%s' is not in the allowlist" % rt, 1)
		return Result.success()

	func _refs() -> RefCounted:
		var root: Dictionary = doc["statements"][doc["root_index"]]
		var head_refs: Array = []
		for a: Dictionary in root["attrs"]:
			GText.collect(a["node"], head_refs, [], [])
		if not head_refs.is_empty():
			return fail("unresolved_ref", "header references a resource", 1)
		for sec: Dictionary in sections():
			var refs: Array = []
			for v: Dictionary in _section_values(sec):
				GText.collect(v, refs, [], [])
			for r: Dictionary in refs:
				var known: Dictionary = ext if r["kind"] == "ExtResource" else sub_ids
				if not known.has(r["id"]):
					return fail("unresolved_ref", "%s(%s) is not defined in this file" % [r["kind"], r["id"]], sec["line"])
		return Result.success()

	func _constructors() -> RefCounted:
		for sec: Dictionary in sections():
			var calls: Array = []
			for v: Dictionary in _section_values(sec):
				GText.collect(v, [], calls, [])
			for c: Dictionary in calls:
				if not Policy.CONSTRUCTORS.has((c["name"] as String).get_slice("[", 0)):
					return fail("constructor_not_allowed", "constructor %s() is not allowed" % c["name"], sec["line"])
		return Result.success()

	## A res:// or uid:// string anywhere else could not be relocated, so it is refused rather than left dangling.
	func _strings() -> RefCounted:
		for sec: Dictionary in sections():
			if sec["section"] == "ext_resource":
				continue
			var strs: Array = []
			for v: Dictionary in _section_values(sec):
				GText.collect(v, [], [], strs)
			if _is_shader_code(sec):
				strs = []
			for s: Dictionary in strs:
				if (s["v"] as String).begins_with("res://") or (s["v"] as String).begins_with("uid://"):
					return fail("unmapped_reference", "a resource reference outside ext_resource cannot be relocated", sec["line"])
		return Result.success()

	func _is_shader_code(sec: Dictionary) -> bool:
		var kind: String = GText.str_attr(sec, "type") if sec["section"] == "sub_resource" else GText.str_attr(doc["statements"][doc["root_index"]], "type")
		return (sec["section"] == "sub_resource" or sec["section"] == "resource") and kind == "Shader"

	func _ext_paths() -> RefCounted:
		var rmap: Dictionary = ck.manifest["resource_map"]
		for rid: String in ext:
			var sec: Dictionary = ext[rid]
			var p: String = GText.str_attr(sec, "path")
			var entry: Variant = rmap.get(p) if not p.is_empty() else null
			if entry == null:
				return fail("unmapped_reference", "ext_resource path '%s' is not in resource_map" % p, sec["line"])
			var scene_like: bool = true
			if entry["kind"] == "package_file":
				paths.append(entry["path"])
				target[rid] = entry["path"]
				scene_like = (entry["path"] as String).ends_with(".glb") or (entry["path"] as String).ends_with(".tscn")
			else:
				deps.append(entry["asset_key"])
				target[rid] = "dep:" + entry["asset_key"]
			if scene_like and GText.str_attr(sec, "type") != "PackedScene":
				return fail("bad_reference_type", "'%s' must be an ext_resource of type PackedScene" % p, sec["line"])
		return Result.success()

	func _nodes() -> RefCounted:
		for sec: Dictionary in sections():
			if sec["section"] != "node":
				continue
			var t: String = GText.str_attr(sec, "type")
			if t.begins_with("CSG"):
				ck.detected["csg_static"] = true
			if t == "CollisionShape3D":
				ck.detected["static_collision"] = true
			var inst: Variant = GText.attr(sec, "instance")
			if inst != null:
				var r: RefCounted = _instance(sec, inst)
				if not r.ok:
					return r
		return Result.success()

	func _instance(sec: Dictionary, inst: Dictionary) -> RefCounted:
		var tgt: String = target.get(inst["id"], "") if inst["t"] == "ref" and inst["kind"] == "ExtResource" else ""
		if tgt.is_empty() or not (tgt.begins_with("dep:") or tgt.ends_with(".tscn") or tgt.ends_with(".glb")):
			return fail("bad_instance", "instance must reference a package .tscn/.glb or an asset dependency", sec["line"])
		if tgt.ends_with(".tscn"):
			instance_paths.append(tgt)
		return Result.success()

	func _inline_shaders() -> RefCounted:
		for sec: Dictionary in sections():
			if not _is_shader_code(sec):
				continue
			for prop: Dictionary in props_of(sec):
				if prop["key"] == "code" and prop["value"]["t"] == "str":
					ck.detected["shader_source"] = true
					var r: RefCounted = ck.check_includes(path, prop["value"]["v"], sec["line"])
					if not r.ok:
						return r
		return Result.success()
