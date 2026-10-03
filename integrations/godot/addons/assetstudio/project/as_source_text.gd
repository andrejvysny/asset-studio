@tool
extends RefCounted
# Publisher pre-flight view of Godot text resources (.tscn/.tres) and shader sources. Parsing is the installer's
# tested, inert parser (as_godot_text.gd); this module only reduces its statements to section headers, header
# attributes and property names and applies the shared policy (as_srcpkg_policy.gd). Nothing is evaluated or
# instantiated; the server repeats the full static validation.

const Policy = preload("res://addons/assetstudio/project/as_srcpkg_policy.gd")
const GText = preload("res://addons/assetstudio/project/as_godot_text.gd")

const MAX_BYTES: int = 8388608


## {"kind": "gd_scene"|"gd_resource"|"", "header": {attr: value}, "sections": [{kind, attrs, line, props}], "bytes": int,
## "error": String (parser message when the text is not a valid text resource, else "")}.
static func scan_text(text: String) -> Dictionary:
	var out: Dictionary = {"kind": "", "header": {}, "sections": [], "bytes": text.length(), "error": ""}
	var parsed: RefCounted = GText.parse_text(text)
	if not parsed.ok:
		out["error"] = parsed.message
		return out
	var current: Dictionary = {}
	for i: int in (parsed.value["statements"] as Array).size():
		var st: Dictionary = parsed.value["statements"][i]
		if st["kind"] == "header" and i == int(parsed.value["root_index"]):
			out["kind"] = st["section"]
			out["header"] = _attrs(st)
		elif st["kind"] == "header":
			current = {"kind": st["section"], "attrs": _attrs(st), "line": int(st["line"]), "props": []}
			(out["sections"] as Array).append(current)
		elif st["kind"] == "prop" and not current.is_empty():
			(current["props"] as Array).append(st["key"])
	return out


## Header attributes as {name: value}: string values unquoted, anything else as its source text.
static func _attrs(header: Dictionary) -> Dictionary:
	var out: Dictionary = {}
	for a: Dictionary in header["attrs"]:
		var node: Dictionary = a["node"]
		out[a["name"]] = node["v"] if node["t"] == "str" else str(a["raw"]).strip_edges()
	return out


## [{"id", "path", "uid", "type", "line"}] for every ext_resource section.
static func ext_resources(scan: Dictionary) -> Array:
	var out: Array = []
	for s: Dictionary in scan["sections"]:
		if s["kind"] == "ext_resource":
			var a: Dictionary = s["attrs"]
			out.append({"id": a.get("id", ""), "path": a.get("path", ""), "uid": a.get("uid", ""),
					"type": a.get("type", ""), "line": s["line"]})
	return out


## Structural problems of one scanned file; `is_scene` selects the expected header. Messages carry `label`.
static func problems(scan: Dictionary, is_scene: bool, label: String) -> PackedStringArray:
	var out := PackedStringArray()
	if str(scan.get("error", "")) != "":
		out.append("%s: %s" % [label, scan["error"]])
		return out
	if scan["kind"] != ("gd_scene" if is_scene else "gd_resource"):
		out.append("%s: not a Godot text %s" % [label, "scene" if is_scene else "resource"])
		return out
	if str((scan["header"] as Dictionary).get("format", "")) not in ["3", "4"]:
		out.append("%s: text format 3 or 4 required" % label)
	if (scan["header"] as Dictionary).has("script_class"):
		out.append("%s: header declares a script class" % label)
	if not is_scene and not Policy.ALLOWED_RESOURCE_TYPES.has(str((scan["header"] as Dictionary).get("type", ""))):
		out.append("%s: resource type '%s' is not supported" % [label, (scan["header"] as Dictionary).get("type", "")])
	for s: Dictionary in scan["sections"]:
		out.append_array(_section_problems(s, label))
	return out


static func _section_problems(s: Dictionary, label: String) -> PackedStringArray:
	var out := PackedStringArray()
	var where: String = "%s:%d" % [label, s["line"]]
	var attrs: Dictionary = s["attrs"]
	if s["kind"] == "connection":
		out.append("%s: signal connections are not supported" % where)
	elif not Policy.SECTION_KINDS.has(s["kind"]):
		out.append("%s: section [%s] is not supported" % [where, s["kind"]])
	if attrs.has("script_class"):
		out.append("%s: declares a script class" % where)
	for key: String in s["props"]:
		if key == "script" or key.begins_with("metadata/_custom_type_script"):
			out.append("%s: scripts are not supported (property '%s')" % [where, key])
	out.append_array(_type_problems(s, where))
	return out


static func _type_problems(s: Dictionary, where: String) -> PackedStringArray:
	var out := PackedStringArray()
	var attrs: Dictionary = s["attrs"]
	var type_name: String = str(attrs.get("type", ""))
	match s["kind"]:
		"ext_resource", "sub_resource":
			if Policy.SCRIPT_TYPES.has(type_name):
				out.append("%s: script resources are not supported" % where)
			elif not Policy.ALLOWED_RESOURCE_TYPES.has(type_name) and not (s["kind"] == "ext_resource" and Policy.EXT_SAVE_CLASSES.has(type_name)):
				out.append("%s: resource type '%s' is not supported" % [where, type_name])
		"node":
			if type_name == "" and not attrs.has("instance"):
				out.append("%s: node has neither a type nor an instance" % where)
			elif type_name != "" and not Policy.ALLOWED_NODE_TYPES.has(type_name):
				out.append("%s: node type '%s' is not supported (animation, skins, particles, custom classes and scripts are blocked)" % [where, type_name])
	return out


## #include arguments of a shader or text body: {"paths": [res:// ...], "bad": [other forms]}.
static func includes(text: String) -> Dictionary:
	var paths := PackedStringArray()
	var bad := PackedStringArray()
	for line: String in text.split("\n"):
		var inc: Dictionary = GText.parse_include(line.strip_edges(false, true))
		if inc.is_empty():
			continue
		if inc["ok"] and str(inc["arg"]).begins_with("res://"):
			paths.append(inc["arg"])
		else:
			bad.append(line.strip_edges().trim_prefix("#").strip_edges().trim_prefix("include").strip_edges())
	return {"paths": paths, "bad": bad}
