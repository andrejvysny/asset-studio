@tool
extends RefCounted
# Wrapper scene writer. `scene_text` (AS-07a) writes the plain wrapper before the model is imported; `build_bytes`
# (AS-08) builds it in memory from the imported model, applies material overrides through a callback and packs it.
# The addon regenerates wrappers and records their hash in .assetstudio/wrappers.json; a wrapper whose file hash
# differs from the record is a conflict and is never overwritten (`check_conflict`).
#
# <prefab_root>/<binding_id>.tscn
#   <BindingName> (Node3D)  metadata: assetstudio_binding, assetstudio_asset_key
#   └─ Model (instance of the managed portable.glb)   position = -placement_anchor
# The model is never re-centred, re-scaled or re-grounded: the node position is the world anchor.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")
const State = preload("res://addons/assetstudio/project/as_project_state.gd")


static func wrapper_rel(prefab_rel: String, binding_id: String) -> String:
	return prefab_rel.path_join("%s.tscn" % binding_id)


## `anchor` = descriptor placement_anchor (three canonical decimal strings); negated textually so no float
## formatting can alter the value.
static func scene_text(binding_id: String, asset_key: String, glb_res_path: String, anchor: Array) -> String:
	var pos: String = "Vector3(%s, %s, %s)" % [_neg(anchor[0]), _neg(anchor[1]), _neg(anchor[2])]
	var node_name: String = binding_id.replace(".", "_")
	return "\n".join([
		"[gd_scene format=3]",
		"",
		"[ext_resource type=\"PackedScene\" path=\"%s\" id=\"1_model\"]" % glb_res_path,
		"",
		"[node name=\"%s\" type=\"Node3D\"]" % node_name,
		"metadata/assetstudio_binding = \"%s\"" % binding_id,
		"metadata/assetstudio_asset_key = \"%s\"" % asset_key,
		"",
		"[node name=\"Model\" parent=\".\" instance=ExtResource(\"1_model\")]",
		"position = %s" % pos,
		"",
	])


static func _neg(decimal: String) -> String:
	if decimal == "0":
		return "0"
	return decimal.substr(1) if decimal.begins_with("-") else "-" + decimal


## Empty string when `wrapper_rel` may be (re)written; otherwise why it must not be.
static func check_conflict(root: String, binding_id: String, wrapper_rel: String) -> String:
	var abs_path: String = root.path_join(wrapper_rel)
	if not FileAccess.file_exists(abs_path):
		return ""
	var rec: Dictionary = State.wrapper_record(root, binding_id)
	if rec.is_empty() or rec.get("path") != wrapper_rel:
		return "%s exists but was not written by the addon" % wrapper_rel
	if rec.get("sha256") != Fs.sha256_file(abs_path):
		return "%s was modified since the addon wrote it" % wrapper_rel
	return ""


## Builds, packs and serializes the wrapper. `model_scene` is the loaded (imported) model; `customize` is called
## with the instantiated Model node (Callable(Node3D) -> ASResult, value {"overrides": int, ...}) and may write
## surface overrides. value = {"bytes": PackedByteArray, "info": <customize value>}.
static func build_bytes(binding_id: String, asset_key: String, model_scene: PackedScene, anchor: Array,
		customize: Callable) -> RefCounted:
	var root := Node3D.new()
	root.name = binding_id.replace(".", "_")
	root.set_meta("assetstudio_binding", binding_id)
	root.set_meta("assetstudio_asset_key", asset_key)
	var model: Node3D = model_scene.instantiate(PackedScene.GEN_EDIT_STATE_INSTANCE) as Node3D
	if model == null:
		root.free()
		return Result.fail("unsupported_representation", "the model scene root is not a Node3D")
	model.name = "Model"
	model.position = Vector3(-float(anchor[0]), -float(anchor[1]), -float(anchor[2]))
	root.add_child(model)
	model.owner = root
	var info: RefCounted = customize.call(model) if customize.is_valid() else Result.success({"overrides": 0})
	var out: RefCounted = info
	if info.ok:
		if int(info.value["overrides"]) > 0:
			root.set_editable_instance(model, true)
		out = _serialize(root, info.value)
	root.free()
	return out


static func _serialize(root: Node3D, info: Dictionary) -> RefCounted:
	var packed := PackedScene.new()
	if packed.pack(root) != OK:
		return Result.fail(Result.CODE_IO_ERROR, "cannot pack the wrapper scene")
	var tmp: String = "user://as_wrapper_%d_%d.tscn" % [Time.get_ticks_usec(), randi()]
	if ResourceSaver.save(packed, tmp) != OK:
		return Result.fail(Result.CODE_IO_ERROR, "cannot serialize the wrapper scene")
	var text: String = FileAccess.get_file_as_string(tmp)
	DirAccess.remove_absolute(ProjectSettings.globalize_path(tmp))
	return Result.success({"bytes": normalize_text(text).to_utf8_buffer(), "info": info})


## Removes what the engine randomizes on every save (node unique ids, uids, ext_resource ids) so that the same
## inputs give the same bytes.
static func normalize_text(text: String) -> String:
	for pattern: String in [" unique_id=[0-9]+", " uid=\"uid://[^\"]*\""]:
		text = RegEx.create_from_string(pattern).sub(text, "", true)
	var ids: PackedStringArray = []
	for m: RegExMatch in RegEx.create_from_string("\\[ext_resource [^\\]]*? id=\"([^\"]+)\"").search_all(text):
		ids.append(m.get_string(1))
	for i: int in ids.size():
		var fresh: String = "%d_ext" % (i + 1)
		text = text.replace("id=\"%s\"" % ids[i], "id=\"%s\"" % fresh).replace(
				"ExtResource(\"%s\")" % ids[i], "ExtResource(\"%s\")" % fresh)
	return text
