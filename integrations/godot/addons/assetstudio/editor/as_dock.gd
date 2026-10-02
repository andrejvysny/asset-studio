@tool
extends VBoxContainer
# The AssetStudio dock (design §8), built in code from native Controls: connection status, library selector,
# server-side search (text, category, tags), asset list (thumbnail, name, exact version, state badge), details
# pane and the actions Install, Place, Review update and Restore previous version. All logic lives in
# as_dock_actions.gd and the project/ modules; the dock must work (and not crash) without any connection.

const Actions = preload("res://addons/assetstudio/editor/as_dock_actions.gd")
const UpdateDialog = preload("res://addons/assetstudio/editor/as_update_dialog.gd")
const DragAdapter = preload("res://addons/assetstudio/editor/as_drag_adapter.gd")
const ChangeWatcher = preload("res://addons/assetstudio/core/as_change_watcher.gd")
const BindingState = preload("res://addons/assetstudio/project/as_binding_state.gd")
const DescriptorDiff = preload("res://addons/assetstudio/project/as_descriptor_diff.gd")
const AssetRef = preload("res://addons/assetstudio/core/as_asset_ref.gd")

const BADGES: Dictionary = {"remote": "Remote", "downloading": "Downloading", "preparing": "Preparing",
		"ready": "Ready", "update_available": "Update available", "unavailable": "Unavailable",
		"unsupported": "Unsupported"}

var actions: Node = null

var _plugin: EditorPlugin = null
var _status: Label = null
var _message: Label = null
var _library: OptionButton = null
var _query: LineEdit = null
var _category: LineEdit = null
var _tags: LineEdit = null
var _list: ItemList = null
var _details: RichTextLabel = null
var _btn_install: Button = null
var _btn_place: Button = null
var _btn_review: Button = null
var _btn_restore: Button = null
var _dialog: ConfirmationDialog = null
var _watcher: Node = null
var _drag := DragAdapter.new()
var _items: Array = []  # server list items, parallel to _list rows (bound-only rows have item["local"] = true)
var _thumbs: Dictionary = {}  # version_id -> Texture2D
var _generation: int = 0


func setup(plugin: EditorPlugin) -> void:
	_plugin = plugin
	name = "AssetStudio"
	actions = Actions.new()
	add_child(actions)
	actions.setup(plugin)
	actions.changed.connect(_render)
	actions.message.connect(func(t: String) -> void: _message.text = t)
	_build()
	_dialog = UpdateDialog.new()
	_dialog.choice.connect(_on_review_choice)
	add_child(_dialog)
	refresh()


func _exit_tree() -> void:
	if _watcher != null:
		_watcher.stop()


# --- construction ------------------------------------------------------------------------------------------------

func _build() -> void:
	_status = Label.new()
	add_child(_status)
	var row := HBoxContainer.new()
	_library = OptionButton.new()
	_library.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_library.item_selected.connect(func(_i: int) -> void: reload_items())
	row.add_child(_library)
	row.add_child(_button("Refresh", refresh))
	row.add_child(_button("Check updates", func() -> void: actions.check_updates()))
	add_child(row)
	_query = _field("Search", reload_items)
	_category = _field("Category id", reload_items)
	_tags = _field("Tags (comma separated)", reload_items)
	_list = ItemList.new()
	_list.size_flags_vertical = Control.SIZE_EXPAND_FILL
	_list.custom_minimum_size = Vector2(0, 160)
	_list.fixed_icon_size = Vector2i(48, 48)
	_list.max_columns = 1
	_list.item_selected.connect(func(_i: int) -> void: _on_select())
	_list.set_drag_forwarding(_drag.get_drag_data.bind(_list), Callable(), Callable())
	add_child(_list)
	_details = RichTextLabel.new()
	_details.custom_minimum_size = Vector2(0, 140)
	_details.fit_content = false
	add_child(_details)
	var buttons := HBoxContainer.new()
	_btn_install = _button("Install", _on_install)
	_btn_place = _button("Place", _on_place)
	_btn_review = _button("Review update", _on_review)
	_btn_restore = _button("Restore previous version", _on_restore)
	for b: Button in [_btn_install, _btn_place, _btn_review, _btn_restore]:
		buttons.add_child(b)
	add_child(buttons)
	_message = Label.new()
	_message.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	add_child(_message)


func _button(text: String, handler: Callable) -> Button:
	var b := Button.new()
	b.text = text
	b.pressed.connect(handler)
	return b


func _field(placeholder: String, on_submit: Callable) -> LineEdit:
	var e := LineEdit.new()
	e.placeholder_text = placeholder
	e.text_submitted.connect(func(_t: String) -> void: on_submit.call())
	add_child(e)
	return e


# --- data --------------------------------------------------------------------------------------------------------

## Full refresh: project state, connection, libraries, items, change watcher.
func refresh() -> void:
	actions.reload()
	var st: Dictionary = actions.connection_status()
	_status.text = st["text"]
	_library.clear()
	if actions.config != null:
		for lib: Dictionary in actions.config.get("libraries"):
			_library.add_item(str(lib["label"]))
			_library.set_item_metadata(_library.item_count - 1, lib["library_id"])
	if st["connected"]:
		_start_watcher()
		await _add_server_libraries()
	reload_items()


func _add_server_libraries() -> void:
	var r: RefCounted = await actions.ensure_client().libraries()
	if not r.ok:
		_message.text = r.describe()
		return
	var known: Array = []
	for i: int in _library.item_count:
		known.append(_library.get_item_metadata(i))
	for lib: Variant in r.value.get("libraries", []):
		if lib is Dictionary and not known.has(lib.get("library_id")):
			_library.add_item(str(lib.get("name", lib.get("library_id"))))
			_library.set_item_metadata(_library.item_count - 1, lib["library_id"])


func _start_watcher() -> void:
	if _watcher != null:
		return
	_watcher = ChangeWatcher.new()
	add_child(_watcher)
	_watcher.changed.connect(_on_events)
	_watcher.start(actions.ensure_client())


func _on_events(events: Array) -> void:
	for e: Variant in events:
		if e is Dictionary and e.get("type") == "asset_current_changed":
			actions.on_asset_current_changed(str(e.get("library_id", "")), str(e.get("asset_id", "")))
		elif e is Dictionary and e.get("type") == "library_changed":
			reload_items()


func _library_id() -> String:
	return str(_library.get_item_metadata(_library.selected)) if _library.selected >= 0 else ""


## Server query (text, category, tags). Without a connection only locally bound assets are listed.
func reload_items() -> void:
	_generation += 1
	var gen: int = _generation
	_items = []
	var lib: String = _library_id()
	if lib != "" and actions.ensure_client() != null:
		var query: Dictionary = {}
		for pair: Array in [["q", _query], ["category", _category], ["tags", _tags]]:
			if (pair[1] as LineEdit).text.strip_edges() != "":
				query[pair[0]] = (pair[1] as LineEdit).text.strip_edges()
		var r: RefCounted = await actions.client.list_assets(lib, query)
		if gen != _generation:
			return
		if r.ok:
			for it: Variant in r.value.get("items", []):
				if it is Dictionary:
					(it as Dictionary)["library_id"] = lib
					_items.append(it)
		else:
			_message.text = r.describe()
	_add_local_only(lib)
	_render()
	_load_thumbnails(gen)


## Bound assets the server did not list (offline, or filtered out) still appear so they stay usable.
func _add_local_only(lib: String) -> void:
	var listed: Dictionary = {}
	for it: Dictionary in _items:
		listed[it["asset_id"]] = true
	for bid: String in actions.infos:
		var i: Dictionary = actions.infos[bid]
		if (lib == "" or i["library_id"] == lib) and not listed.has(i["asset_id"]):
			listed[i["asset_id"]] = true
			_items.append({"asset_id": i["asset_id"], "library_id": i["library_id"], "display_name": bid,
					"current_version_id": i["version_id"], "display_version": i["version_id"], "local": true})


func _render() -> void:
	if _list == null:
		return
	var keep: int = _selected_index()
	_list.clear()
	for it: Dictionary in _items:
		var state: String = BindingState.item_state(it, actions.infos, actions.transient)
		var entry: Dictionary = _entry_for(it, state)
		var idx: int = _list.add_item("%s   %s   [%s]" % [it.get("display_name", it["asset_id"]),
				it.get("display_version", it["current_version_id"]), BADGES.get(state, state)])
		_list.set_item_metadata(idx, entry)
		if _thumbs.has(it["current_version_id"]):
			_list.set_item_icon(idx, _thumbs[it["current_version_id"]])
	if keep >= 0 and keep < _list.item_count:
		_list.select(keep)
	_update_buttons()
	_show_details()


## List entry understood by the drag adapter: {"state", "wrapper_res", "name", "binding_id", "item"}.
func _entry_for(it: Dictionary, state: String) -> Dictionary:
	var bid: String = _binding_for(it)
	var wrapper: String = actions.infos[bid]["wrapper_res"] if bid != "" else ""
	var bstate: String = actions.infos[bid]["state"] if bid != "" else state
	if bid != "" and actions.infos[bid]["version_id"] != it["current_version_id"] and bstate == BindingState.READY:
		bstate = BindingState.UPDATE
	return {"state": bstate if bid != "" else state, "wrapper_res": wrapper, "name": it.get("display_name", ""),
			"binding_id": bid, "item": it, "badge": state}


## The binding of the item: the one at its current version, else any binding of the asset.
func _binding_for(it: Dictionary) -> String:
	var found: String = ""
	for bid: String in actions.bindings_of(it["asset_id"], it["library_id"]):
		if actions.infos[bid]["version_id"] == it["current_version_id"]:
			return bid
		found = bid if found == "" else found
	return found


func _load_thumbnails(gen: int) -> void:
	for it: Dictionary in _items:
		if gen != _generation or actions.client == null:
			return
		var ver: String = str(it.get("current_version_id", ""))
		if not it.get("has_thumbnail", false) or _thumbs.has(ver):
			continue
		var r: RefCounted = await actions.client.thumbnail_bytes(it["library_id"], it["asset_id"], ver)
		if r.ok:
			var img := Image.new()
			if img.load_png_from_buffer(r.value["bytes"]) == OK or img.load_jpg_from_buffer(r.value["bytes"]) == OK \
					or img.load_webp_from_buffer(r.value["bytes"]) == OK:
				_thumbs[ver] = ImageTexture.create_from_image(img)
	if gen == _generation:
		_render()


# --- selection and actions ---------------------------------------------------------------------------------------

func _selected_index() -> int:
	var sel: PackedInt32Array = _list.get_selected_items() if _list != null else PackedInt32Array()
	return sel[0] if not sel.is_empty() else -1


func _selected_entry() -> Dictionary:
	var i: int = _selected_index()
	return _list.get_item_metadata(i) if i >= 0 else {}


func _on_select() -> void:
	_update_buttons()
	_show_details()


func _update_buttons() -> void:
	var e: Dictionary = _selected_entry()
	var bid: String = e.get("binding_id", "")
	var state: String = e.get("badge", "")
	var installable: PackedStringArray = [BindingState.REMOTE, BindingState.UNSUPPORTED, BindingState.UNAVAILABLE,
			BindingState.UPDATE]
	_btn_install.disabled = e.is_empty() or e["item"].get("local", false) or not installable.has(state)
	_btn_place.disabled = not BindingState.is_placeable(e.get("state", ""))
	_btn_review.disabled = bid == "" or actions.infos.get(bid, {}).get("target_version", "") == ""
	_btn_restore.disabled = bid == "" or not actions.can_restore_previous(bid)


func _on_install() -> void:
	var e: Dictionary = _selected_entry()
	if not e.is_empty():
		await actions.install(e["item"])


func _on_place() -> void:
	var e: Dictionary = _selected_entry()
	if not e.is_empty() and e["binding_id"] != "":
		actions.place(e["binding_id"])


func _on_review() -> void:
	var e: Dictionary = _selected_entry()
	if e.is_empty() or e["binding_id"] == "":
		return
	var r: RefCounted = await actions.review(e["binding_id"])
	if r.ok:
		_dialog.show_review(r.value)
	else:
		_message.text = r.describe()


func _on_review_choice(mode: String) -> void:
	var e: Dictionary = _selected_entry()
	if e.is_empty() or e["binding_id"] == "":
		return
	match mode:
		"binding":
			await actions.apply_update_binding(e["binding_id"])
		"instances":
			await actions.apply_update_instances(e["binding_id"])
		_:
			actions.dismiss(e["binding_id"])


func _on_restore() -> void:
	var e: Dictionary = _selected_entry()
	if not e.is_empty() and e["binding_id"] != "":
		await actions.restore_previous(e["binding_id"])


func _show_details() -> void:
	var e: Dictionary = _selected_entry()
	if e.is_empty():
		_details.text = "Select an asset."
		return
	var it: Dictionary = e["item"]
	var lines: PackedStringArray = ["%s" % it.get("display_name", it["asset_id"]),
			"Version: %s (%s)" % [it.get("display_version", ""), it["current_version_id"]], "State: %s" % BADGES.get(e["badge"], e["badge"])]
	for bid: String in actions.bindings_of(it["asset_id"], it["library_id"]):
		var i: Dictionary = actions.infos[bid]
		var pol: String = str(actions.lock.bindings()[bid]["material_policy"]["mode"])
		lines.append("Binding %s: %s, %s, material policy %s" % [bid, i["version_id"], BADGES.get(i["state"], i["state"]), pol])
	_details.text = "\n".join(lines)
	_describe_async(it, _generation)


## Descriptor summary (bounds, anchor, scale, slots, warnings) fetched through the resolve route.
func _describe_async(it: Dictionary, gen: int) -> void:
	if actions.client == null or it.get("local", false):
		return
	var ref: RefCounted = AssetRef.parse({"server_id": actions.config.get("server_id"), "library_id": it["library_id"],
			"asset_id": it["asset_id"], "version_id": it["current_version_id"]}).value
	var r: RefCounted = await DescriptorDiff.fetch_descriptor(actions.client, ref)
	if gen != _generation or not r.ok or _selected_entry().get("item", {}).get("asset_id") != it["asset_id"]:
		return
	var d: Dictionary = r.value.data
	var slots: PackedStringArray = []
	for s: Dictionary in d["material_slots"]:
		slots.append("%s (%s)" % [s["slot_id"], s["role"]])
	_details.text += "\nAnchor %s  Bounds %s .. %s\nScale %s  Height %s\nSlots: %s\nCollision: %s\nWarnings: %s" % [
			str(d["placement_anchor"]), str(d["bounds_min"]), str(d["bounds_max"]), str(d["scale_range"]),
			str(d["height_offset_range_m"]), ", ".join(slots) if not slots.is_empty() else "none",
			"none" if d["collision"] == null else str(d["collision"]["shape_types"]),
			", ".join(PackedStringArray(d["preview_warnings"])) if not (d["preview_warnings"] as Array).is_empty() else "none"]
