@tool
extends RefCounted
# Portable GLB export of the frozen graph (design 5.6). The graph is the collector's private, disk-loaded duplicate
# and is consumed here: CSG trees are replaced by their baked meshes (the editable construction stays in the
# source package), ShaderMaterials by disclosed StandardMaterial3D approximations, and collision shapes / markers
# are removed (collision is source-only). GLTFDocument writes a self-contained GLB which is parsed back and
# verified (no external uris, skins or animations); iPad budgets are reported as warnings, never as blocks.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Graph = preload("res://addons/assetstudio/project/as_source_graph.gd")
const GlbInspect = preload("res://addons/assetstudio/project/as_glb_inspect.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")

const COLOR_PARAMS: PackedStringArray = ["albedo", "albedo_color", "base_color", "color", "tint"]
const REASON_TINTED: String = "custom shader replaced by a tinted StandardMaterial3D in the portable GLB"
const REASON_NEUTRAL: String = "custom shader replaced by a neutral StandardMaterial3D in the portable GLB"


## Consumes collection["graph"]. value = {"path", "sha256", "facts", "slots": [{slot_id, role, portable: [{mesh,
## primitive}], source_surfaces}], "approximations", "warnings", "omissions"}.
static func export_glb(collection: Dictionary, out_path: String) -> RefCounted:
	var root: Node3D = collection["graph"]
	root.transform = Transform3D.IDENTITY
	var slots: Array = collection["slots"]
	var swapped: RefCounted = _swap_csg(root, collection["baked"])
	if not swapped.ok:
		return swapped
	var approx: Dictionary = _approximate(root, collection["baked"], slots)
	_strip(root)
	var bound: Array = _bind_refs(root, slots)
	_unique_names(root)
	var state := GLTFState.new()
	var doc := GLTFDocument.new()
	if doc.append_from_scene(root, state) != OK:
		return Result.fail("unsupported_source", "GLTFDocument could not export the scene")
	var glb: PackedByteArray = doc.generate_buffer(state)
	var facts: Dictionary = GlbInspect.inspect(glb)
	if not facts["ok"]:
		return Result.fail("unsafe_package", "the portable export is not a static self-contained GLB", false,
				{"problems": facts["problems"]})
	if Fs.write_atomic(out_path, glb) != OK:
		return Result.fail(Result.CODE_IO_ERROR, "cannot write %s" % out_path)
	var resolved: RefCounted = _resolve_surfaces(slots, bound, facts)
	if not resolved.ok:
		return resolved
	var warnings: Array = GlbInspect.budget_warnings(facts)
	return Result.success({"path": out_path, "sha256": Fs.sha256_bytes(glb), "facts": facts, "slots": resolved.value,
			"approximations": approx["list"], "warnings": warnings, "omissions": _omissions(collection)})


static func _omissions(collection: Dictionary) -> Array:
	return ["static collision shapes are source-only and are not part of portable.glb"] if collection["collision"] != null else []


# --- graph rewrites ------------------------------------------------------------------------------------------

static func _swap_csg(root: Node3D, baked: Dictionary) -> RefCounted:
	var paths: Array = baked.keys()
	paths.sort()
	for p: String in paths:
		if p == ".":
			return Result.fail("unsupported_source", "the scene root is a CSG shape: wrap the CSG tree in a Node3D")
		var csg: Node3D = root.get_node(p) as Node3D
		var parent: Node = csg.get_parent()
		var index: int = csg.get_index()
		var mi := MeshInstance3D.new()
		mi.mesh = baked[p]
		mi.transform = csg.transform
		parent.remove_child(csg)
		mi.name = csg.name
		parent.add_child(mi)
		parent.move_child(mi, index)
		csg.free()
	return Result.success()


## Replaces every ShaderMaterial surface with an approximation; value = {"list": [{slot_id, reason}]}.
static func _approximate(root: Node, baked: Dictionary, slots: Array) -> Dictionary:
	var made: Dictionary = {}
	for mi: MeshInstance3D in _mesh_instances(root):
		for s: int in mi.mesh.get_surface_count() if mi.mesh != null else 0:
			var mat: Material = mi.get_active_material(s)
			if not mat is ShaderMaterial:
				continue
			if not made.has(mat):
				made[mat] = _approximation(mat)
			if mi.material_override is ShaderMaterial:
				mi.material_override = made[mat]["material"]
			elif mi.mesh is ArrayMesh and baked.values().has(mi.mesh):
				(mi.mesh as ArrayMesh).surface_set_material(s, made[mat]["material"])
			else:
				mi.set_surface_override_material(s, made[mat]["material"])
	var list: Array = []
	for slot: Dictionary in slots:
		if slot["shader"]:
			list.append({"slot_id": slot["slot_id"], "reason": REASON_TINTED if made.get(slot["material"], {}).get("tinted", false) else REASON_NEUTRAL})
	return {"list": list}


static func _approximation(mat: ShaderMaterial) -> Dictionary:
	var colors: Dictionary = {}
	if mat.shader != null:
		for u: Dictionary in mat.shader.get_shader_uniform_list():
			if int(u["type"]) == TYPE_COLOR and mat.get_shader_parameter(u["name"]) is Color:
				colors[str(u["name"])] = mat.get_shader_parameter(u["name"])
	var chosen: String = ""
	for preferred: String in COLOR_PARAMS:
		if colors.has(preferred):
			chosen = preferred
			break
	if chosen == "" and not colors.is_empty():
		var names: Array = colors.keys()
		names.sort()
		chosen = names[0]
	var std := StandardMaterial3D.new()
	std.resource_name = mat.resource_name
	if chosen != "":
		std.albedo_color = colors[chosen]
	return {"material": std, "tinted": chosen != ""}


static func _mesh_instances(n: Node) -> Array:
	var out: Array = []
	if n is MeshInstance3D and (n as MeshInstance3D).mesh != null:
		out.append(n)
	for c: Node in n.get_children():
		out.append_array(_mesh_instances(c))
	return out


## Removes markers and collision shapes; bodies become plain Node3D (collision is source-only).
static func _strip(n: Node) -> void:
	for c: Node in n.get_children():
		if c is Marker3D or c is CollisionShape3D:
			n.remove_child(c)
			c.free()
		elif c is StaticBody3D:
			_strip(_plain(n, c as StaticBody3D))
		else:
			_strip(c)


static func _plain(parent: Node, body: StaticBody3D) -> Node3D:
	var plain := Node3D.new()
	plain.transform = body.transform
	var index: int = body.get_index()
	for c: Node in body.get_children():
		body.remove_child(c)
		plain.add_child(c)
	parent.remove_child(body)
	plain.name = body.name
	body.free()
	parent.add_child(plain)
	parent.move_child(plain, index)
	return plain


## Node references behind every portable ref (resolved before names are touched).
static func _bind_refs(root: Node, slots: Array) -> Array:
	var out: Array = []
	for slot: Dictionary in slots:
		var nodes: Array = []
		for r: Dictionary in slot["portable_refs"]:
			nodes.append({"node": root.get_node_or_null(r["path"]), "surface": r["surface"]})
		out.append(nodes)
	return out


## GLTF node names must be unique for the name based surface lookup below.
static func _unique_names(root: Node) -> void:
	var used: Dictionary = {}
	var stack: Array = [root]
	while not stack.is_empty():
		var n: Node = stack.pop_front()
		var base: String = String(n.name)
		var candidate: String = base
		var k: int = 2
		while used.has(candidate):
			candidate = "%s_%d" % [base, k]
			k += 1
		used[candidate] = true
		if candidate != base:
			n.name = candidate
		stack.append_array(n.get_children())


static func _resolve_surfaces(slots: Array, bound: Array, facts: Dictionary) -> RefCounted:
	var out: Array = []
	var by_node: Dictionary = facts["mesh_of_node"]
	for i: int in slots.size():
		var seen: Dictionary = {}
		var portable: Array = []
		for b: Dictionary in bound[i]:
			var node: Node = b["node"]
			if node == null or not by_node.has(String(node.name)):
				return Result.fail("unsupported_source", "slot '%s': a surface did not reach the portable GLB" % slots[i]["slot_id"])
			var mesh: int = int(by_node[String(node.name)])
			if int(b["surface"]) >= int(facts["primitives"][mesh]):
				return Result.fail("unsupported_source", "slot '%s': surface %d has no matching primitive" % [slots[i]["slot_id"], int(b["surface"])])
			var key: String = "%d:%d" % [mesh, int(b["surface"])]
			if not seen.has(key):
				seen[key] = true
				portable.append({"mesh": mesh, "primitive": int(b["surface"])})
		portable.sort_custom(func(a: Dictionary, c: Dictionary) -> bool: return [a["mesh"], a["primitive"]] < [c["mesh"], c["primitive"]])
		out.append({"slot_id": slots[i]["slot_id"], "role": slots[i]["role"], "portable": portable,
				"source_surfaces": slots[i]["source_surfaces"], "shader": slots[i]["shader"]})
	return Result.success(out)
