extends RefCounted
# Minimal wrapper scene writer (AS-07a). Material overrides, slot resolution and conflict handling are AS-08.
#
# <prefab_root>/<binding_id>.tscn
#   <BindingName> (Node3D)  metadata: assetstudio_binding, assetstudio_asset_key
#   └─ Model (instance of the managed portable.glb)   position = -placement_anchor
# The model is never re-centred, re-scaled or re-grounded: the node position is the world anchor.


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
