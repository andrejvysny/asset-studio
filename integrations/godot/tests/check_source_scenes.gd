extends SceneTree
# Used by run_source_tests.py. Usage: godot --headless --path <project> --script res://check_source_scenes.gd
# Loads every installed entry scene (installed.json), instantiates it and prints "STATE {json}" with what was
# resolved (meshes, materials, textures, shaders, instanced dependency) plus the foreign UID registry state.

const FOREIGN_UIDS: PackedStringArray = ["uid://c8w2q1prop001", "uid://c8w2q1prop002", "uid://b3k1m0pa1n7ro", "uid://b3k1m0bark001",
		"uid://b3k1m0leaf001", "uid://d4t8x1bark001", "uid://d4t8x1leaf001", "uid://b3k1m0cryst01", "uid://f6s3h1cryst01",
		"uid://c8w2q1cryst01", "uid://c8w2q1hut0001", "uid://c8w2q1tree001", "uid://c8w2q1clust01", "uid://c8w2q1rock001",
		"uid://e5r2k1rock001", "uid://c8w2q1prop001"]


func _init() -> void:
	var installed: Dictionary = JSON.parse_string(FileAccess.get_file_as_string("res://installed.json"))
	var state: Dictionary = {"scenes": {}, "foreign_uids_registered": []}
	for name: String in installed:
		state["scenes"][name] = _describe(installed[name]["entry"])
	for uid: String in FOREIGN_UIDS:
		if ResourceUID.has_id(ResourceUID.text_to_id(uid)):
			state["foreign_uids_registered"].append(uid)
	print("STATE " + JSON.stringify(state))
	quit(0)


func _describe(path: String) -> Dictionary:
	var scene: PackedScene = load(path) as PackedScene
	if scene == null:
		return {"error": "cannot load " + path}
	var inst: Node = scene.instantiate()
	var out: Dictionary = {"root": inst.name, "nodes": {}}
	_walk(inst, inst, out["nodes"])
	inst.free()
	return out


func _walk(root: Node, n: Node, out: Dictionary) -> void:
	var info: Dictionary = {"type": n.get_class()}
	if n is MeshInstance3D:
		var mi: MeshInstance3D = n
		info["mesh"] = mi.mesh.get_class() if mi.mesh != null else null
		info["materials"] = []
		for i: int in maxi(1, mi.get_surface_override_material_count()):
			info["materials"].append(_material(mi.get_surface_override_material(i)))
	if n is MeshInstance3D and (n as MeshInstance3D).mesh is ArrayMesh:
		var am: ArrayMesh = (n as MeshInstance3D).mesh
		info["array_surfaces"] = []
		for i: int in am.get_surface_count():
			info["array_surfaces"].append({"vertices": am.surface_get_arrays(i)[Mesh.ARRAY_VERTEX].size(),
					"material": _material(am.surface_get_material(i))})
	if n is CollisionShape3D and (n as CollisionShape3D).shape != null:
		info["shape"] = (n as CollisionShape3D).shape.get_class()
	out[str(root.get_path_to(n))] = info
	for c: Node in n.get_children():
		_walk(root, c, out)


func _material(m: Material) -> Variant:
	if m == null:
		return null
	var d: Dictionary = {"class": m.get_class()}
	if m is StandardMaterial3D:
		var sm: StandardMaterial3D = m
		d["albedo"] = sm.albedo_color.to_html(false)
		d["texture"] = sm.albedo_texture.get_width() if sm.albedo_texture != null else 0
	if m is ShaderMaterial and (m as ShaderMaterial).shader != null:
		d["shader_code"] = (m as ShaderMaterial).shader.code.length() > 0
	return d
