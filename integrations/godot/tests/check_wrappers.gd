extends SceneTree
# Consumer-project check used by run_consumer_tests.py (copied next to project.godot).
# Usage: godot --headless --path <project> --script res://check_wrappers.gd -- <res://wrapper.tscn>=<ax>,<ay>,<az> ...
# Loads each wrapper scene, instantiates it and requires: a MeshInstance3D under Model, and Model.position == -anchor.


func _init() -> void:
	var failures: PackedStringArray = PackedStringArray()
	for arg: String in OS.get_cmdline_user_args():
		var path: String = arg.get_slice("=", 0)
		var a: PackedStringArray = arg.get_slice("=", 1).split(",")
		var err: String = _check(path, Vector3(a[0].to_float(), a[1].to_float(), a[2].to_float()))
		print("%s %s %s" % ["FAIL" if err != "" else "OK", path, err])
		if err != "":
			failures.append(path)
	print("CHECK_%s" % ("FAILED" if not failures.is_empty() else "OK"))
	quit(1 if not failures.is_empty() else 0)


func _check(path: String, anchor: Vector3) -> String:
	var scene: PackedScene = load(path) as PackedScene
	if scene == null:
		return "cannot load scene"
	var inst: Node = scene.instantiate()
	var model: Node3D = inst.get_node_or_null("Model") as Node3D
	if model == null:
		return "no Model child"
	if not model.position.is_equal_approx(-anchor):
		return "Model.position %s != -anchor %s" % [str(model.position), str(-anchor)]
	if _first_mesh(model) == null:
		return "no MeshInstance3D under Model"
	if str(inst.get_meta("assetstudio_binding", "")) != path.get_file().get_basename():
		return "binding metadata missing"
	inst.free()
	return ""


func _first_mesh(n: Node) -> MeshInstance3D:
	if n is MeshInstance3D:
		return n
	for c: Node in n.get_children():
		var m: MeshInstance3D = _first_mesh(c)
		if m != null:
			return m
	return null
