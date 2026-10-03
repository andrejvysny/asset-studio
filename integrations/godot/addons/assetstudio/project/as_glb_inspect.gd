@tool
extends RefCounted
# Static GLB inspection: container + JSON chunk only (no scene is built). Used to check that a produced or
# packaged .glb is self-contained and static, and to report the iPad structural budgets as warnings.

const Policy = preload("res://addons/assetstudio/project/as_srcpkg_policy.gd")

const MAGIC: int = 0x46546C67
const CHUNK_JSON: int = 0x4E4F534A
const CHUNK_BIN: int = 0x004E4942


## {"ok", "problems", "doc", "bytes", "nodes", "meshes", "materials", "triangles", "max_texture_px", "images",
## "has_uv", "has_normal", "has_color", "mesh_of_node": {node name: mesh index}, "primitives": [count per mesh]}.
static func inspect(glb: PackedByteArray) -> Dictionary:
	var out: Dictionary = {"ok": false, "problems": [], "doc": {}, "bytes": glb.size(), "nodes": 0,
			"meshes": 0, "materials": 0, "triangles": 0, "max_texture_px": 0, "images": 0, "has_uv": false,
			"has_normal": false, "has_color": false, "mesh_of_node": {}, "primitives": []}
	var chunks: Dictionary = _chunks(glb)
	if chunks.is_empty():
		(out["problems"] as Array).append("not a valid GLB container")
		return out
	var parsed: Variant = JSON.parse_string((chunks["json"] as PackedByteArray).get_string_from_utf8())
	if not parsed is Dictionary:
		(out["problems"] as Array).append("GLB JSON chunk is not an object")
		return out
	out["doc"] = parsed
	_structure(parsed, out)
	_budget(parsed, chunks["bin"], out)
	out["ok"] = (out["problems"] as Array).is_empty()
	return out


static func _chunks(glb: PackedByteArray) -> Dictionary:
	if glb.size() < 20 or glb.decode_u32(0) != MAGIC or glb.decode_u32(4) != 2 or int(glb.decode_u32(8)) != glb.size():
		return {}
	var out: Dictionary = {"json": PackedByteArray(), "bin": PackedByteArray()}
	var off: int = 12
	var seen_json: bool = false
	while off + 8 <= glb.size():
		var length: int = glb.decode_u32(off)
		var kind: int = glb.decode_u32(off + 4)
		if off + 8 + length > glb.size():
			return {}
		if kind == CHUNK_JSON and not seen_json:
			out["json"] = glb.slice(off + 8, off + 8 + length)
			seen_json = true
		elif kind == CHUNK_BIN and (out["bin"] as PackedByteArray).is_empty():
			out["bin"] = glb.slice(off + 8, off + 8 + length)
		off += 8 + length + ((4 - length % 4) % 4)
	return out if seen_json else {}


static func _structure(doc: Dictionary, out: Dictionary) -> void:
	var problems: Array = out["problems"]
	for kind: String in ["buffers", "images"]:
		for i: int in (doc.get(kind, []) as Array).size():
			var uri: Variant = doc[kind][i].get("uri") if doc[kind][i] is Dictionary else null
			if uri != null and not str(uri).begins_with("data:"):
				problems.append("%s[%d] references an external uri" % [kind, i])
	if not (doc.get("skins", []) as Array).is_empty():
		problems.append("the GLB contains skins")
	if not (doc.get("animations", []) as Array).is_empty():
		problems.append("the GLB contains animations")
	if not (doc.get("extensionsRequired", []) as Array).is_empty():
		problems.append("the GLB requires extensions: %s" % str(doc["extensionsRequired"]))
	out["nodes"] = (doc.get("nodes", []) as Array).size()
	out["meshes"] = (doc.get("meshes", []) as Array).size()
	out["materials"] = (doc.get("materials", []) as Array).size()
	out["images"] = (doc.get("images", []) as Array).size()


static func _budget(doc: Dictionary, bin: PackedByteArray, out: Dictionary) -> void:
	var meshes: Array = doc.get("meshes", [])
	var accessors: Array = doc.get("accessors", [])
	for m: Variant in meshes:
		(out["primitives"] as Array).append(((m as Dictionary).get("primitives", []) as Array).size())
		for prim: Variant in (m as Dictionary).get("primitives", []):
			var attrs: Dictionary = (prim as Dictionary).get("attributes", {})
			out["has_uv"] = out["has_uv"] or attrs.has("TEXCOORD_0")
			out["has_normal"] = out["has_normal"] or attrs.has("NORMAL")
			out["has_color"] = out["has_color"] or attrs.has("COLOR_0")
	var tris: int = 0
	for node: Variant in doc.get("nodes", []):
		var mesh_index: int = int((node as Dictionary).get("mesh", -1))
		if mesh_index >= 0 and mesh_index < meshes.size():
			(out["mesh_of_node"] as Dictionary)[str((node as Dictionary).get("name", ""))] = mesh_index
			tris += _triangles(meshes[mesh_index], accessors)
	out["triangles"] = tris
	out["max_texture_px"] = _max_texture(doc, bin)


static func _triangles(mesh: Dictionary, accessors: Array) -> int:
	var total: int = 0
	for prim: Variant in mesh.get("primitives", []):
		var p: Dictionary = prim
		if int(p.get("mode", 4)) != 4:
			continue
		var acc: int = int(p["indices"]) if p.has("indices") else int((p.get("attributes", {}) as Dictionary).get("POSITION", -1))
		if acc >= 0 and acc < accessors.size():
			total += int((accessors[acc] as Dictionary).get("count", 0)) / 3
	return total


static func _max_texture(doc: Dictionary, bin: PackedByteArray) -> int:
	var best: int = 0
	var views: Array = doc.get("bufferViews", [])
	for img: Variant in doc.get("images", []):
		var view_index: int = int((img as Dictionary).get("bufferView", -1))
		if view_index < 0 or view_index >= views.size():
			continue
		var view: Dictionary = views[view_index]
		var start: int = int(view.get("byteOffset", 0))
		var data: PackedByteArray = bin.slice(start, start + int(view.get("byteLength", 0)))
		if data.size() >= 24 and data.decode_u32(0) == 0x474E5089:
			best = maxi(best, maxi(_be32(data, 16), _be32(data, 20)))
		else:
			var image := Image.new()
			if image.load_jpg_from_buffer(data) == OK or image.load_webp_from_buffer(data) == OK:
				best = maxi(best, maxi(image.get_width(), image.get_height()))
	return best


static func _be32(data: PackedByteArray, off: int) -> int:
	return (data[off] << 24) | (data[off + 1] << 16) | (data[off + 2] << 8) | data[off + 3]


## iPad budget overruns as human-readable lines (warnings, never blocks).
static func budget_warnings(facts: Dictionary) -> Array:
	var out: Array = []
	var checks: Array = [["triangles", Policy.IPAD_MAX_TRIANGLES], ["nodes", Policy.IPAD_MAX_NODES],
			["materials", Policy.IPAD_MAX_MATERIALS], ["max_texture_px", Policy.IPAD_MAX_TEXTURE_PX],
			["bytes", Policy.IPAD_GLB_MAX_BYTES]]
	for c: Array in checks:
		if int(facts[c[0]]) > int(c[1]):
			out.append("portable %s %d exceeds the iPad budget of %d" % [c[0], int(facts[c[0]]), int(c[1])])
	return out
