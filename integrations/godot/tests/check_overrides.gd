extends SceneTree
# Consumer-project check used by run_consumer_tests.py (copied next to project.godot).
# Usage: godot --headless --path <project> --script res://check_overrides.gd -- <res://wrapper.tscn> ...
# Prints one line "STATE <json>": per wrapper the Model position and, for every MeshInstance3D under Model, the
# surface override materials (null when absent) by node path relative to Model.


func _init() -> void:
	var out: Dictionary = {}
	for path: String in OS.get_cmdline_user_args():
		var packed: PackedScene = load(path) as PackedScene
		if packed == null:
			out[path] = null
			continue
		var inst: Node = packed.instantiate()
		var model: Node3D = inst.get_node("Model") as Node3D
		var meshes: Dictionary = {}
		_walk(model, model, meshes)
		out[path] = {"position": [model.position.x, model.position.y, model.position.z], "meshes": meshes,
				"binding": str(inst.get_meta("assetstudio_binding", "")), "asset_key": str(inst.get_meta("assetstudio_asset_key", ""))}
		inst.free()
	print("STATE " + JSON.stringify(out))
	quit()


func _walk(n: Node, model: Node, out: Dictionary) -> void:
	if n is MeshInstance3D:
		var mi: MeshInstance3D = n
		var surfaces: Array = []
		for i: int in mi.mesh.get_surface_count():
			var m: Material = mi.get_surface_override_material(i)
			if m == null:
				surfaces.append(null)
			elif m is BaseMaterial3D:
				var b: BaseMaterial3D = m
				surfaces.append({"roughness": snappedf(b.roughness, 0.001), "metallic": snappedf(b.metallic, 0.001),
						"albedo": b.albedo_color.to_html(false), "path": b.resource_path})
			else:
				surfaces.append({"other": m.get_class()})
		out[str(model.get_path_to(n))] = surfaces
	for c: Node in n.get_children():
		_walk(c, model, out)
