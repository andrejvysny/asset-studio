@tool
extends RefCounted
# Static graph analysis of the frozen (instantiated, duplicated) scene: allowlist check, geometry bounds, ground
# anchor, material slots with stable ids, collision shapes and detected capabilities. The graph must already be
# inside a SceneTree and have had two frames so CSG shapes are built. Nothing here mutates the graph.
#
# Source surfaces: a MeshInstance3D reports (its path, surface index); an instanced .glb is opaque to the source
# format, so all its surfaces report (the instance node's path, running index). CSG nodes with a material report
# (their path, 0); portable surfaces of CSG slots point at the baked mesh of the CSG root.

const Policy = preload("res://addons/assetstudio/project/as_srcpkg_policy.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")

const GROUND_ANCHOR: String = "GroundAnchor"


## value keys: slots, aabb (AABB), has_geometry, anchor (Vector3), has_anchor, shapes (Array[String]), caps
## (Dictionary String -> true), baked (path -> ArrayMesh), problems (PackedStringArray), shader_slots (Array).
static func analyze(root: Node) -> Dictionary:
	var ctx: Dictionary = {"slots": [], "slot_index": {}, "used_ids": {}, "aabb": AABB(), "has_geometry": false,
			"anchor": Vector3.ZERO, "has_anchor": false, "shapes": [], "caps": {}, "baked": {},
			"problems": [], "counters": {}}
	if not root is Node3D:
		(ctx["problems"] as Array).append("the scene root must be a Node3D")
		return ctx
	_visit(root, ".", Transform3D.IDENTITY, "", "", ctx)
	_finish(ctx)
	return ctx


static func _problem(ctx: Dictionary, msg: String) -> void:
	(ctx["problems"] as Array).append(msg)


static func _visit(n: Node, path: String, xf: Transform3D, opaque: String, csg_root: String, ctx: Dictionary) -> void:
	if n.get_script() != null:
		_problem(ctx, "node '%s' has a script attached (scripts are not supported)" % path)
	var inside: String = opaque
	if inside == "" and path != "." and _is_opaque_instance(n):
		inside = path
		(ctx["counters"] as Dictionary)[path] = 0
	_check_class(n, path, inside, ctx)
	var next_csg: String = csg_root
	if n is MeshInstance3D:
		_mesh(n, path, xf, inside, ctx)
	elif n is CSGShape3D:
		next_csg = _csg(n, path, xf, csg_root, ctx)
	elif inside == "":
		_special(n, path, xf, ctx)
	for c: Node in n.get_children():
		var cxf: Transform3D = xf * (c as Node3D).transform if c is Node3D else xf
		_visit(c, String(c.name) if path == "." else path + "/" + String(c.name), cxf, inside, next_csg, ctx)


static func _is_opaque_instance(n: Node) -> bool:
	var ext: String = n.scene_file_path.get_extension().to_lower()
	return ext == "glb" or ext == "gltf"


static func _check_class(n: Node, path: String, inside: String, ctx: Dictionary) -> void:
	var cls: String = n.get_class()
	if inside == "":
		if not Policy.ALLOWED_NODE_TYPES.has(cls):
			_problem(ctx, "node '%s' (%s) is not supported: animation, skins, particles, custom classes and scripts block publication" % [path, cls])
		return
	for blocked: String in Policy.NON_STATIC_CLASSES:
		if n.is_class(blocked):
			_problem(ctx, "node '%s' (%s) inside an imported model is animated, skinned or a particle system" % [path, cls])
	if n is MeshInstance3D and (n as MeshInstance3D).skin != null:
		_problem(ctx, "mesh '%s' inside an imported model is skinned" % path)


static func _special(n: Node, path: String, xf: Transform3D, ctx: Dictionary) -> void:
	if n is CollisionShape3D:
		_shape(n, path, ctx)
	elif n is Marker3D and n.name == GROUND_ANCHOR and not ctx["has_anchor"]:
		ctx["anchor"] = xf.origin
		ctx["has_anchor"] = true


static func _shape(n: CollisionShape3D, path: String, ctx: Dictionary) -> void:
	(ctx["caps"] as Dictionary)["static_collision"] = true
	var kinds: Dictionary = {"BoxShape3D": "box", "SphereShape3D": "sphere", "CapsuleShape3D": "capsule",
			"CylinderShape3D": "cylinder", "ConvexPolygonShape3D": "convex", "ConcavePolygonShape3D": "concave"}
	if n.shape == null or not kinds.has(n.shape.get_class()):
		_problem(ctx, "collision shape '%s' is empty or of an unsupported type" % path)
		return
	(ctx["shapes"] as Array).append(kinds[n.shape.get_class()])


static func _grow(ctx: Dictionary, box: AABB) -> void:
	ctx["aabb"] = (ctx["aabb"] as AABB).merge(box) if ctx["has_geometry"] else box
	ctx["has_geometry"] = true


# --- meshes and CSG ------------------------------------------------------------------------------------------

static func _mesh(mi: MeshInstance3D, path: String, xf: Transform3D, opaque: String, ctx: Dictionary) -> void:
	var mesh: Mesh = mi.mesh
	if mesh == null:
		return
	_grow(ctx, xf * mesh.get_aabb())
	var counters: Dictionary = ctx["counters"]
	for s: int in mesh.get_surface_count():
		var mat: Material = mi.get_active_material(s)
		_flags(mesh, s, mat, ctx)
		var source_path: String = opaque if opaque != "" else path
		var source_surface: int = s
		if opaque != "":
			source_surface = int(counters[opaque])
			counters[opaque] = source_surface + 1
		if mat != null:
			_add_surface(ctx, mat, source_path, source_surface, path, s, mi.name)


## Returns the CSG root path the children belong to.
static func _csg(n: CSGShape3D, path: String, xf: Transform3D, csg_root: String, ctx: Dictionary) -> String:
	(ctx["caps"] as Dictionary)["csg_static"] = true
	var root_path: String = csg_root
	if n.is_root_shape():
		root_path = path
		var baked: ArrayMesh = n.bake_static_mesh()
		if baked == null or baked.get_surface_count() == 0:
			_problem(ctx, "CSG tree '%s' produced no geometry" % path)
			return root_path
		(ctx["baked"] as Dictionary)[path] = baked
		_grow(ctx, xf * baked.get_aabb())
		for s: int in baked.get_surface_count():
			_flags(baked, s, baked.surface_get_material(s), ctx)
	var mat: Material = n.get("material") as Material
	if mat == null or not (ctx["baked"] as Dictionary).has(root_path):
		return root_path
	var surface: int = _baked_surface(ctx["baked"][root_path], mat)
	if surface < 0:
		_problem(ctx, "material of CSG node '%s' is not part of the baked mesh" % path)
	else:
		_add_surface(ctx, mat, path, 0, root_path, surface, n.name)
	return root_path


static func _baked_surface(baked: ArrayMesh, mat: Material) -> int:
	for s: int in baked.get_surface_count():
		if baked.surface_get_material(s) == mat:
			return s
	return -1


static func _flags(mesh: Mesh, surface: int, mat: Material, ctx: Dictionary) -> void:
	var caps: Dictionary = ctx["caps"]
	if mesh is ArrayMesh and ((mesh as ArrayMesh).surface_get_format(surface) & Mesh.ARRAY_FORMAT_COLOR) != 0:
		caps["vertex_colors"] = true
	if mat is ShaderMaterial:
		caps["shader_source"] = true
	elif mat is BaseMaterial3D:
		var b: BaseMaterial3D = mat
		if b.vertex_color_use_as_albedo:
			caps["vertex_colors"] = true
		if b.transparency == BaseMaterial3D.TRANSPARENCY_ALPHA_SCISSOR:
			caps["alpha_mask"] = true
		elif b.transparency != BaseMaterial3D.TRANSPARENCY_DISABLED:
			caps["alpha_blend"] = true
		for t: int in BaseMaterial3D.TEXTURE_MAX:
			if b.get_texture(t as BaseMaterial3D.TextureParam) != null:
				caps["pbr_textures"] = true


# --- material slots ------------------------------------------------------------------------------------------

static func _add_surface(ctx: Dictionary, mat: Material, source_path: String, source_surface: int,
		export_path: String, export_surface: int, node_name: String) -> void:
	var key: String = _material_key(mat)
	var index: Dictionary = ctx["slot_index"]
	if not index.has(key):
		var base: String = slugify(_name_hint(mat, node_name, source_surface), "material")
		var slot_id: String = _unique(base, ctx["used_ids"])
		var slot: Dictionary = {"slot_id": slot_id, "role": base, "key": key, "material": mat,
				"shader": mat is ShaderMaterial, "source_surfaces": [], "portable_refs": []}
		index[key] = slot
		(ctx["slots"] as Array).append(slot)
	var found: Dictionary = index[key]
	var source: Dictionary = {"node_path": source_path, "surface": source_surface}
	if not (found["source_surfaces"] as Array).has(source):
		(found["source_surfaces"] as Array).append(source)
	(found["portable_refs"] as Array).append({"path": export_path, "surface": export_surface})


static func _material_key(mat: Material) -> String:
	var p: String = mat.resource_path
	return "res:" + p if p != "" and not p.contains("::") else "inst:%d" % mat.get_instance_id()


static func _name_hint(mat: Material, node_name: String, surface: int) -> String:
	if mat.resource_name != "":
		return mat.resource_name
	var p: String = mat.resource_path
	if p != "" and not p.contains("::"):
		return p.get_file().get_basename()
	return node_name if surface == 0 else "%s_%d" % [node_name, surface]


static func slugify(text: String, fallback: String) -> String:
	var out: String = ""
	for ch: String in text.to_lower():
		out += ch if (ch >= "a" and ch <= "z") or (ch >= "0" and ch <= "9") or ch == "_" or ch == "." or ch == "-" else "_"
	while out.begins_with("_") or out.begins_with(".") or out.begins_with("-"):
		out = out.substr(1)
	out = out.left(60)
	return out if out != "" else fallback


static func _unique(base: String, used: Dictionary) -> String:
	var candidate: String = base
	var n: int = 2
	while used.has(candidate):
		candidate = "%s_%d" % [base, n]
		n += 1
	used[candidate] = true
	return candidate


static func _finish(ctx: Dictionary) -> void:
	if not ctx["has_geometry"]:
		_problem(ctx, "the scene has no geometry (MeshInstance3D or CSG)")
	if (ctx["slots"] as Array).size() > Policy.MAX_SLOTS:
		_problem(ctx, "more than %d material slots" % Policy.MAX_SLOTS)
	for slot: Dictionary in ctx["slots"]:
		if (slot["source_surfaces"] as Array).size() > Policy.MAX_SURFACES_PER_SLOT:
			_problem(ctx, "slot '%s' has more than %d surfaces" % [slot["slot_id"], Policy.MAX_SURFACES_PER_SLOT])
		for s: Dictionary in slot["source_surfaces"]:
			if not Schema.matches("node_path", s["node_path"]):
				_problem(ctx, "node path '%s' uses characters outside A-Z a-z 0-9 _ - (rename the node)" % s["node_path"])


# --- placement -----------------------------------------------------------------------------------------------

## Canonical decimal string (at most 5 fractional digits, no trailing zeros, never "-0").
static func decimal(v: float) -> String:
	var text: String = "%.5f" % v
	if text.contains("."):
		text = text.rstrip("0").rstrip(".")
	return "0" if text == "-0" or text == "" else text


## {"anchor": [3 decimals], "footprint_radius_m": decimal} from the analysis.
static func placement_values(ctx: Dictionary) -> Dictionary:
	var box: AABB = ctx["aabb"]
	var anchor: Vector3 = ctx["anchor"] if ctx["has_anchor"] else Vector3(box.get_center().x, box.position.y, box.get_center().z)
	var far: float = 0.0
	for corner: int in 8:
		var p: Vector3 = box.get_endpoint(corner)
		far = maxf(far, Vector2(p.x - anchor.x, p.z - anchor.z).length())
	var radius: float = maxf(0.1, ceilf(far * 10.0) / 10.0)
	return {"anchor": [decimal(anchor.x), decimal(anchor.y), decimal(anchor.z)], "footprint_radius_m": decimal(radius)}
