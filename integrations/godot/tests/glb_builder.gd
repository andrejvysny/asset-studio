extends RefCounted
# Test helper (not a test file): builds GLB bytes and imported-like scenes at runtime with GLTFDocument.

## ArrayMesh with one triangle surface per name; an empty name leaves the material unnamed.
static func mesh_with(material_names: Array) -> ArrayMesh:
	var am := ArrayMesh.new()
	for i: int in material_names.size():
		var arr: Array = []
		arr.resize(Mesh.ARRAY_MAX)
		arr[Mesh.ARRAY_VERTEX] = PackedVector3Array([Vector3(0, 0, i), Vector3(1, 0, i), Vector3(0, 1, i)])
		am.add_surface_from_arrays(Mesh.PRIMITIVE_TRIANGLES, arr)
		var m := StandardMaterial3D.new()
		m.resource_name = material_names[i]
		am.surface_set_material(i, m)
	return am


## GLB bytes of a scene with one MeshInstance3D named `node_name` carrying `mesh`.
static func glb_of(mesh: Mesh, node_name: String) -> PackedByteArray:
	var root := Node3D.new()
	var mi := MeshInstance3D.new()
	mi.mesh = mesh
	mi.name = node_name
	root.add_child(mi)
	mi.owner = root
	var state := GLTFState.new()
	var doc := GLTFDocument.new()
	doc.append_from_scene(root, state)
	var bytes: PackedByteArray = doc.generate_buffer(state)
	root.free()
	return bytes


## Instantiates a scene from GLB bytes the way the runtime importer does (the editor importer prefixes mesh names
## with the node name as well, which is what the slot resolver's suffix rule is for).
static func scene_of(glb: PackedByteArray) -> Node:
	var state := GLTFState.new()
	var doc := GLTFDocument.new()
	if doc.append_from_buffer(glb, "", state) != OK:
		return null
	return doc.generate_scene(state)
