@tool
extends RefCounted
# Derived, relocated source tree (spec §8). A validated package is rewritten with a tested line-oriented
# parser/serializer, never with a global text replacement:
#   (a) ext_resource `path=` values go through the resource map (package_file -> res://<delivery>/source/<path>;
#       asset_dependency -> the installed entrypoint of that dependency's delivery),
#   (b) `uid=` is removed from the gd_scene/gd_resource header and from every ext_resource (foreign UIDs would
#       collide when two versions share them; the local path fallback stays valid),
#   (c) shader `#include "res://..."` lines (also inside an inline Shader `code` value) go through the same map.
# Statements that are not changed keep their original bytes. Binary members are copied byte-identical.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")
const Zip = preload("res://addons/assetstudio/project/as_srcpkg_zip.gd")
const Policy = preload("res://addons/assetstudio/project/as_srcpkg_policy.gd")
const GText = preload("res://addons/assetstudio/project/as_godot_text.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")
const Package = preload("res://addons/assetstudio/project/as_source_package.gd")

const TREE_DIR: String = "source"


static func _unmapped(ref: String) -> RefCounted:
	return Result.fail("unsupported_source_dependency", "reference %s has no resource_map entry" % ref, false,
			{"detail": "unmapped_reference", "path": ref})


## original reference -> new res:// path. `delivery_res` = "res://<managed>/<key>/<hash>"; `dependency_res` maps an
## asset_key to the same form for its delivery. value = Dictionary.
static func build_map(manifest: Dictionary, delivery_res: String, dependency_res: Dictionary) -> RefCounted:
	var map: Dictionary = {}
	var rmap: Dictionary = manifest["resource_map"]
	for ref: String in rmap:
		var e: Dictionary = rmap[ref]
		if e["kind"] == "package_file":
			map[ref] = "%s/%s/%s" % [delivery_res, TREE_DIR, e["path"]]
		elif dependency_res.has(e["asset_key"]):
			map[ref] = "%s/%s" % [dependency_res[e["asset_key"]], e["entrypoint"]]
		else:
			return Result.fail("unsupported_source_dependency", "dependency %s is not available" % str(e["asset_key"]).left(12),
					false, {"detail": "dependency_not_in_closure", "path": ref})
	return Result.success(map)


## value = {"text": String, "changed": bool}.
static func rewrite_text(text: String, map: Dictionary) -> RefCounted:
	var parsed: RefCounted = GText.parse_text(text)
	if not parsed.ok:
		return parsed
	var doc: Dictionary = parsed.value
	var out := PackedStringArray()
	var current: Dictionary = {}
	var statements: Array = doc["statements"]
	for i: int in statements.size():
		var s: Dictionary = statements[i]
		var raw: String = s["raw"]
		if s["kind"] == "header":
			current = s
			var h: RefCounted = _rewrite_header(s, i == int(doc["root_index"]), map)
			if not h.ok:
				return h
			raw = h.value
		elif s["kind"] == "prop" and _is_shader_code(current, doc, s):
			var p: RefCounted = _rewrite_code_prop(s, map)
			if not p.ok:
				return p
			raw = p.value
		out.append(raw)
	var result: String = "\n".join(out)
	return Result.success({"text": result, "changed": result != text})


static func _is_shader_code(header: Dictionary, doc: Dictionary, prop: Dictionary) -> bool:
	if prop["key"] != "code" or prop["value"]["t"] != "str" or header.is_empty():
		return false
	var root: Dictionary = doc["statements"][doc["root_index"]]
	if header["section"] == "sub_resource":
		return GText.str_attr(header, "type") == "Shader"
	return header["section"] == "resource" and GText.str_attr(root, "type") == "Shader"


## Header raw text after dropping uid (root and ext_resource) and mapping an ext_resource path.
static func _rewrite_header(s: Dictionary, is_root: bool, map: Dictionary) -> RefCounted:
	var kind: String = s["section"]
	if not is_root and kind != "ext_resource":
		return Result.success(s["raw"])
	if kind == "ext_resource" and GText.attr(s, "path") == null:
		return _unmapped("")
	var attrs: Array = []
	var changed: bool = false
	for a: Dictionary in s["attrs"]:
		var item: Dictionary = a
		if a["name"] == "uid":
			changed = true
			continue
		if kind == "ext_resource" and a["name"] == "path":
			var old: String = a["node"]["v"] if a["node"]["t"] == "str" else ""
			if not map.has(old):
				return _unmapped(old)
			item = {"name": "path", "raw": GText.quote(map[old])}
			changed = true
		attrs.append(item)
	if not changed:
		return Result.success(s["raw"])
	var text: String = GText.header_text(kind, attrs)
	return Result.success(text + ("\r" if (s["raw"] as String).ends_with("\r") else ""))


static func _rewrite_code_prop(s: Dictionary, map: Dictionary) -> RefCounted:
	var r: RefCounted = rewrite_shader(s["value"]["v"], map)
	if not r.ok:
		return r
	if not r.value["changed"]:
		return Result.success(s["raw"])
	return Result.success("%s = %s" % [s["key"], GText.quote(r.value["text"])])


## Shader source (or inline code). `res://` includes go through the map; relative includes stay as they are.
static func rewrite_shader(text: String, map: Dictionary) -> RefCounted:
	var lines: PackedStringArray = text.split("\n")
	var changed: bool = false
	for i: int in lines.size():
		var inc: Dictionary = GText.parse_include(lines[i])
		if inc.is_empty():
			continue
		if not inc["ok"]:
			return Zip.unsafe("shader_include_escape", "malformed #include", "")
		if (inc["arg"] as String).begins_with("res://"):
			if not map.has(inc["arg"]):
				return _unmapped(inc["arg"])
			lines[i] = inc["head"] + GText.quote(map[inc["arg"]]) + inc["tail"]
			changed = true
	return Result.success({"text": "\n".join(lines), "changed": changed})


## Writes the derived tree of `zip_path` below `out_dir` (member paths are kept). Every member is re-read and
## re-checked against the manifest first, so the tree is derived from exactly the validated bytes.
## value = [{"path", "sha256", "size", "original_sha256", "rewritten"}] in manifest order.
static func relocate(zip_path: String, manifest: Dictionary, out_dir: String, map: Dictionary) -> RefCounted:
	var opened: RefCounted = Zip.open(zip_path)
	if not opened.ok:
		return opened
	var zip: RefCounted = opened.value
	var out: Array = []
	var result: RefCounted = Result.success(out)
	for f: Dictionary in manifest["files"]:
		var one: RefCounted = _relocate_member(zip, f, out_dir, map)
		if not one.ok:
			result = one
			break
		out.append(one.value)
	zip.call("close")
	return result


static func _relocate_member(zip: RefCounted, f: Dictionary, out_dir: String, map: Dictionary) -> RefCounted:
	var data: RefCounted = zip.call("read", f["path"])
	if not data.ok:
		return data
	var bytes: PackedByteArray = data.value
	if Canonical.sha256_hex(bytes) != f["sha256"] or bytes.size() != int(f["size"]):
		return Result.fail("integrity_mismatch", "member changed after validation", false, {"detail": "sha256", "path": f["path"]})
	var suffix: String = Package.suffix_of(f["path"])
	var is_text: bool = suffix == ".tscn" or suffix == ".tres"
	var is_shader: bool = suffix == ".gdshader" or suffix == ".gdshaderinc"
	if is_text or is_shader:
		var text: String = bytes.get_string_from_utf8()
		var rw: RefCounted = rewrite_text(text, map) if is_text else rewrite_shader(text, map)
		if not rw.ok:
			return rw
		if rw.value["changed"]:
			bytes = (rw.value["text"] as String).to_utf8_buffer()
	if Fs.write_atomic(out_dir.path_join(f["path"]), bytes) != OK:
		return Result.fail(Result.CODE_IO_ERROR, "cannot write %s" % f["path"])
	var sha: String = Canonical.sha256_hex(bytes)
	return Result.success({"path": f["path"], "sha256": sha, "size": bytes.size(), "original_sha256": f["sha256"],
			"rewritten": sha != f["sha256"]})
