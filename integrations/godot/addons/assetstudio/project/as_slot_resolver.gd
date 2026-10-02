@tool
extends RefCounted
# Material slot resolution (design §5.1). Descriptor slot surfaces are glTF {mesh, primitive} indices; this maps
# them to (node path relative to the model root, surface index) in the imported scene.
#
# The GLB bytes (already verified against the receipt) give the glTF mesh names and primitive counts. The imported
# scene gives MeshInstance3D nodes. A glTF mesh is matched to an imported Mesh by resource_name (the importer
# prefixes the node name: "<node>_<mesh>", so an exact match or a "_<mesh>" suffix match counts) and the surface
# count must equal the glTF primitive count. Whatever stays unmatched falls back to first appearance in the node
# tree. A surface that still cannot be resolved is reported as unresolved and keeps its source material.
# Surface p == primitive p only because the installer pre-seeds array_mesh/deduplicate_surfaces=false.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")

const GLB_MAGIC: int = 0x46546C67
const CHUNK_JSON: int = 0x4E4F534A
const REPRESENTATION: String = "portable_glb_v1"


## value = Array of {"name": String, "primitives": int}, indexed by glTF mesh index.
static func gltf_meshes(glb: PackedByteArray) -> RefCounted:
	if glb.size() < 20 or glb.decode_u32(0) != GLB_MAGIC or glb.decode_u32(4) != 2 \
			or glb.decode_u32(8) != glb.size():
		return Result.fail("unsafe_package", "not a GLB 2.0 container")
	var length: int = glb.decode_u32(12)
	if glb.decode_u32(16) != CHUNK_JSON or length > glb.size() - 20:
		return Result.fail("unsafe_package", "GLB has no JSON chunk")
	var json := JSON.new()
	if json.parse(glb.slice(20, 20 + length).get_string_from_utf8()) != OK or not json.data is Dictionary:
		return Result.fail("unsafe_package", "GLB JSON chunk is not valid JSON")
	var out: Array = []
	var meshes: Variant = (json.data as Dictionary).get("meshes", [])
	if not meshes is Array:
		return Result.fail("unsafe_package", "GLB meshes is not an array")
	for m: Variant in meshes:
		if not m is Dictionary:
			return Result.fail("unsafe_package", "GLB mesh entry is not an object")
		var prims: Variant = (m as Dictionary).get("primitives", [])
		out.append({"name": str((m as Dictionary).get("name", "")),
				"primitives": (prims as Array).size() if prims is Array else 0})
	return Result.success(out)


## `meshes` = gltf_meshes().value, `root` = instantiated imported scene, `slots` = descriptor material_slots.
## value = {"slots": {slot_id: [{"path": String, "surface": int}]}, "unresolved": [{"slot_id", "reason"}]}.
## "path" is relative to `root` ("." is the root itself).
static func resolve(meshes: Array, root: Node, slots: Array) -> RefCounted:
	var nodes: Array[MeshInstance3D] = []
	_collect(root, nodes)
	var assigned: Dictionary = _assign(meshes, nodes)
	var out: Dictionary = {}
	var unresolved: Array = []
	for s: Variant in slots:
		var slot: Dictionary = s
		var targets: Array = []
		var items: Variant = (slot["surfaces"] as Dictionary).get(REPRESENTATION)
		if not items is Array:
			unresolved.append({"slot_id": slot["slot_id"], "reason": "no %s surfaces" % REPRESENTATION})
		else:
			for item: Dictionary in items:
				var reason: String = _targets(item, assigned, nodes, root, targets)
				if reason != "":
					unresolved.append({"slot_id": slot["slot_id"], "reason": reason})
		out[slot["slot_id"]] = targets
	return Result.success({"slots": out, "unresolved": unresolved})


static func _collect(n: Node, out: Array[MeshInstance3D]) -> void:
	if n is MeshInstance3D and (n as MeshInstance3D).mesh != null:
		out.append(n)
	for c: Node in n.get_children():
		_collect(c, out)


## glTF mesh index -> Mesh (only the ones that could be matched).
static func _assign(meshes: Array, nodes: Array[MeshInstance3D]) -> Dictionary:
	var distinct: Array[Mesh] = []
	for n: MeshInstance3D in nodes:
		if not distinct.has(n.mesh):
			distinct.append(n.mesh)
	var assigned: Dictionary = {}
	var claimed: Array[Mesh] = []
	for gi: int in meshes.size():
		var m: Mesh = _by_name(meshes, gi, distinct, claimed)
		if m != null:
			assigned[gi] = m
			claimed.append(m)
	var free_meshes: Array[Mesh] = distinct.filter(func(m: Mesh) -> bool: return not claimed.has(m))
	for gi: int in meshes.size():
		if assigned.has(gi):
			continue
		for cand: Mesh in free_meshes:
			if cand.get_surface_count() == int(meshes[gi]["primitives"]):
				assigned[gi] = cand
				free_meshes.erase(cand)
				break
	return assigned


static func _by_name(meshes: Array, gi: int, distinct: Array[Mesh], claimed: Array[Mesh]) -> Mesh:
	var name: String = meshes[gi]["name"]
	if name.is_empty() or meshes.filter(func(m: Dictionary) -> bool: return m["name"] == name).size() > 1:
		return null
	var found: Array[Mesh] = []
	for m: Mesh in distinct:
		if claimed.has(m) or m.get_surface_count() != int(meshes[gi]["primitives"]):
			continue
		if m.resource_name == name or m.resource_name.ends_with("_" + name):
			found.append(m)
	if found.size() > 1:
		var exact: Array[Mesh] = found.filter(func(m: Mesh) -> bool: return m.resource_name == name)
		return exact[0] if exact.size() == 1 else null
	return found[0] if found.size() == 1 else null


## Appends the targets of one {mesh, primitive}; returns "" or the reason it cannot be resolved.
static func _targets(item: Dictionary, assigned: Dictionary, nodes: Array[MeshInstance3D], root: Node,
		out: Array) -> String:
	var mi: int = int(item["mesh"])
	var prim: int = int(item["primitive"])
	if not assigned.has(mi):
		return "glTF mesh %d not found in the imported scene" % mi
	var mesh: Mesh = assigned[mi]
	if prim >= mesh.get_surface_count():
		return "glTF mesh %d has no primitive %d" % [mi, prim]
	for n: MeshInstance3D in nodes:
		if n.mesh == mesh:
			out.append({"path": str(root.get_path_to(n)), "surface": prim})
	return ""
