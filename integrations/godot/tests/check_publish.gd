extends SceneTree
# Consumer-project check used by run_publish_tests.py (copied next to project.godot).
# Usage: godot --headless --path <project> --script res://check_publish.gd -- <portable.glb> ...
# Re-imports each GLB with GLTFDocument (as the importer would) and prints one `GLBFACTS <json>` line per file:
# node/mesh/surface/material counts, normals, UVs, vertex colors, textures and alpha modes per surface.


func _init() -> void:
	for path: String in OS.get_cmdline_user_args():
		var bytes: PackedByteArray = FileAccess.get_file_as_bytes(path)
		var state := GLTFState.new()
		var doc := GLTFDocument.new()
		if doc.append_from_buffer(bytes, "", state) != OK:
			print("GLBFACTS ", JSON.stringify({"path": path, "error": "cannot import"}))
			continue
		var root: Node = doc.generate_scene(state)
		var facts: Dictionary = {"path": path, "nodes": 0, "mesh_nodes": 0, "surfaces": [], "materials": []}
		_walk(root, facts)
		print("GLBFACTS ", JSON.stringify(facts))
		root.free()
	quit()


func _walk(n: Node, facts: Dictionary) -> void:
	facts["nodes"] += 1
	if n is MeshInstance3D:
		facts["mesh_nodes"] += 1
		var mesh: Mesh = (n as MeshInstance3D).mesh
		for s: int in mesh.get_surface_count():
			var arrays: Array = mesh.surface_get_arrays(s)
			var mat: Material = mesh.surface_get_material(s)
			var info: Dictionary = {"node": String(n.name), "surface": s,
					"normal": (arrays[Mesh.ARRAY_NORMAL] as PackedVector3Array).size() > 0 if arrays[Mesh.ARRAY_NORMAL] != null else false,
					"uv": (arrays[Mesh.ARRAY_TEX_UV] as PackedVector2Array).size() > 0 if arrays[Mesh.ARRAY_TEX_UV] != null else false,
					"color": (arrays[Mesh.ARRAY_COLOR] as PackedColorArray).size() > 0 if arrays[Mesh.ARRAY_COLOR] != null else false,
					"material": mat.get_class() if mat != null else ""}
			if mat is BaseMaterial3D:
				info["texture"] = (mat as BaseMaterial3D).albedo_texture != null
				info["transparency"] = (mat as BaseMaterial3D).transparency
				info["albedo"] = (mat as BaseMaterial3D).albedo_color.to_html(false)
			(facts["surfaces"] as Array).append(info)
	for c: Node in n.get_children():
		_walk(c, facts)
