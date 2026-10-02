@tool
extends RefCounted
# Scene edits made through an undo manager: place a wrapper instance, swap wrapper instances to another wrapper.
# `undo` is an EditorUndoRedoManager in the editor (with the edited scene root as context) or a plain UndoRedo in
# tests; the two differ in create_action / add_*_method signatures, which the helpers below hide.

const WRAPPER_META: String = "assetstudio_binding"


## Instantiates `packed` under `parent` at the world position `world_pos`. Returns the new node.
static func place(undo: Object, scene_root: Node, parent: Node, packed: PackedScene, world_pos: Vector3) -> Node:
	var inst: Node = packed.instantiate(PackedScene.GEN_EDIT_STATE_INSTANCE)
	if inst is Node3D:
		var local: Vector3 = world_pos
		if parent is Node3D and (parent as Node3D).is_inside_tree():
			local = (parent as Node3D).global_transform.affine_inverse() * world_pos
		(inst as Node3D).position = local
	_begin(undo, "Place AssetStudio asset", scene_root)
	_do(undo, parent, "add_child", [inst, true])
	_do(undo, inst, "set_owner", [scene_root])
	undo.add_do_reference(inst)
	_undo(undo, parent, "remove_child", [inst])
	_undo(undo, inst, "set_owner", [null])
	undo.commit_action()
	return inst


## Replaces each of `olds` by an instance of `packed` at the same index, name and transform. Returns the new nodes.
static func swap_instances(undo: Object, scene_root: Node, olds: Array, packed: PackedScene) -> Array:
	var news: Array = []
	_begin(undo, "Update AssetStudio instances", scene_root)
	for old: Node in olds:
		var parent: Node = old.get_parent()
		var idx: int = old.get_index()
		var fresh: Node = packed.instantiate(PackedScene.GEN_EDIT_STATE_INSTANCE)
		fresh.name = old.name
		if fresh is Node3D and old is Node3D:
			(fresh as Node3D).transform = (old as Node3D).transform
		_do(undo, parent, "remove_child", [old])
		_do(undo, old, "set_owner", [null])
		_do(undo, parent, "add_child", [fresh])
		_do(undo, parent, "move_child", [fresh, idx])
		_do(undo, fresh, "set_owner", [scene_root])
		undo.add_do_reference(fresh)
		_undo(undo, parent, "remove_child", [fresh])
		_undo(undo, fresh, "set_owner", [null])
		_undo(undo, parent, "add_child", [old])
		_undo(undo, parent, "move_child", [old, idx])
		_undo(undo, old, "set_owner", [scene_root])
		undo.add_undo_reference(old)
		news.append(fresh)
	undo.commit_action()
	return news


## Wrapper instances (nodes carrying assetstudio_binding == binding_id) among `nodes`.
static func instances_of(nodes: Array, binding_id: String) -> Array:
	return nodes.filter(func(n: Node) -> bool: return str(n.get_meta(WRAPPER_META, "")) == binding_id)


static func _begin(undo: Object, name: String, scene_root: Node) -> void:
	if undo is UndoRedo:
		undo.create_action(name)
	else:
		undo.create_action(name, UndoRedo.MERGE_DISABLE, scene_root)


static func _do(undo: Object, obj: Object, method: String, args: Array) -> void:
	if undo is UndoRedo:
		undo.add_do_method(Callable(obj, method).bindv(args))
	else:
		undo.callv("add_do_method", [obj, method] + args)


static func _undo(undo: Object, obj: Object, method: String, args: Array) -> void:
	if undo is UndoRedo:
		undo.add_undo_method(Callable(obj, method).bindv(args))
	else:
		undo.callv("add_undo_method", [obj, method] + args)
