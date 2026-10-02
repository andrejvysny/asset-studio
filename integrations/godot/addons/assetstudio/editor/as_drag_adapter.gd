@tool
extends RefCounted
# Drag adapter (design §8): dragging a Ready asset from the dock list yields Godot's native file-drop payload,
# so placement and Undo are owned by the editor's own drop handling. Anything not Ready returns null (no drag).
# Hooked up with ItemList.set_drag_forwarding(adapter.get_drag_data.bind(list), Callable(), Callable()).

const BindingState = preload("res://addons/assetstudio/project/as_binding_state.gd")


## `entry` = the dock list entry {"state", "wrapper_res", "name"}. null unless the binding is Ready (an
## "update available" binding is still Ready: its installed version works) and its wrapper exists.
static func payload_for(entry: Variant) -> Variant:
	if not entry is Dictionary or not BindingState.is_placeable(str(entry.get("state", ""))):
		return null
	var path: String = str(entry.get("wrapper_res", ""))
	if not path.begins_with("res://") or not FileAccess.file_exists(path):
		return null
	return {"type": "files", "files": PackedStringArray([path])}


func get_drag_data(at_position: Vector2, list: ItemList) -> Variant:
	var idx: int = list.get_item_at_position(at_position, true)
	if idx < 0:
		return null
	var entry: Variant = list.get_item_metadata(idx)
	var payload: Variant = payload_for(entry)
	if payload != null:
		var label := Label.new()
		label.text = str(entry.get("name", "asset"))
		list.set_drag_preview(label)
	return payload
