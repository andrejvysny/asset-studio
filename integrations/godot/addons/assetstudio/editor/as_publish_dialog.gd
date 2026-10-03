@tool
extends ConfirmationDialog
# Publish review dialog (design 5.6). Stage 1 collects name, category, tags, licence and the target; stage 2 shows
# the server validation, the conversion omissions/approximations and the descriptor draft and only then offers the
# explicit Commit. A stale base version shows the conflict and offers "Publish as new asset" (the local source is
# never touched). The dialog only reports choices; as_publish_actions.gd runs them.

signal preview_requested(options: Dictionary)
signal commit_requested
signal new_asset_requested

const STAGE_FORM: String = "form"
const STAGE_REVIEW: String = "review"

var stage: String = STAGE_FORM
var _name: LineEdit = null
var _category: LineEdit = null
var _tags: LineEdit = null
var _licence: LineEdit = null
var _target: CheckBox = null
var _selection: CheckBox = null
var _fields: VBoxContainer = null
var _body: RichTextLabel = null
var _btn_new: Button = null


func _init() -> void:
	title = "Publish to AssetStudio"
	ok_button_text = "Build and preview"
	cancel_button_text = "Close"
	_btn_new = add_button("Publish as new asset", false, "new_asset")
	var box := VBoxContainer.new()
	_fields = VBoxContainer.new()
	_name = _field("Name")
	_category = _field("Category id (optional)")
	_tags = _field("Tags (comma separated)")
	_licence = _field("Licence (default unknown)")
	_target = CheckBox.new()
	_selection = CheckBox.new()
	_selection.text = "Publish the selected node only"
	_fields.add_child(_target)
	_fields.add_child(_selection)
	_body = RichTextLabel.new()
	_body.custom_minimum_size = Vector2(620, 320)
	_body.bbcode_enabled = false
	box.add_child(_fields)
	box.add_child(_body)
	add_child(box)
	confirmed.connect(_on_confirmed)
	custom_action.connect(func(action: StringName) -> void:
		if action == &"new_asset":
			new_asset_requested.emit())
	get_ok_button().disabled = false
	_btn_new.hide()


func _field(placeholder: String) -> LineEdit:
	var e := LineEdit.new()
	e.placeholder_text = placeholder
	_fields.add_child(e)
	return e


## defaults: {"name", "asset": {"display_name", "asset_id", "current_version_id"} or {}, "selection": bool}.
func open_form(defaults: Dictionary) -> void:
	stage = STAGE_FORM
	_name.text = str(defaults.get("name", ""))
	var asset: Dictionary = defaults.get("asset", {})
	_target.visible = not asset.is_empty()
	_target.button_pressed = not asset.is_empty()
	_target.text = "Publish as a new version of '%s' (base version %s)" % [asset.get("display_name", ""), asset.get("current_version_id", "")] if not asset.is_empty() else ""
	_selection.visible = bool(defaults.get("selection", false))
	_selection.button_pressed = false
	_fields.show()
	_body.text = ""
	ok_button_text = "Build and preview"
	_btn_new.hide()
	popup_centered()


func form_options() -> Dictionary:
	return {"name": _name.text.strip_edges(), "category": _category.text.strip_edges(), "tags": _tags.text.strip_edges(),
			"licence": _licence.text.strip_edges(), "new_version": _target.visible and _target.button_pressed,
			"selection": _selection.visible and _selection.button_pressed}


func show_progress(text: String) -> void:
	_body.text = text
	get_ok_button().disabled = true


func show_review(review: Dictionary) -> void:
	stage = STAGE_REVIEW
	_fields.hide()
	_body.text = review_text(review)
	ok_button_text = "Commit"
	get_ok_button().disabled = false
	_btn_new.visible = review["target"]["mode"] == "new_version"
	popup_centered()


## `conflict` = the stale_pointer details ({"current_version_id"}) or {}.
func show_error(text: String, conflict: Dictionary = {}) -> void:
	_fields.hide()
	_body.text = text
	get_ok_button().disabled = true
	_btn_new.visible = not conflict.is_empty()
	popup_centered()


func _on_confirmed() -> void:
	if stage == STAGE_FORM:
		preview_requested.emit(form_options())
	else:
		commit_requested.emit()


## Plain-text review of ASPublishCommand.prepare()["review"].
static func review_text(review: Dictionary) -> String:
	var t: Dictionary = review["target"]
	var lines := PackedStringArray()
	lines.append("%s '%s' in library %s" % ["New version of %s (base %s)" % [t["target_asset_id"], t["expected_current_version"]] if t["mode"] == "new_version" else "New asset", t["name"], t["library"]])
	lines.append("Capabilities: %s" % ", ".join(PackedStringArray(review["capabilities"])))
	lines.append("")
	lines.append("Server validation (preview %s):" % review["preview_id"])
	var warnings: Array = (review["server"]["warnings"] as Array) + (review["local_warnings"] as Array)
	lines.append("  warnings: %s" % (", ".join(PackedStringArray(warnings)) if not warnings.is_empty() else "none"))
	lines.append("  iPad budget: %s" % review["server"].get("budget"))
	lines.append("")
	lines.append_array(_report_lines(review["conversion_report"]))
	var draft: Dictionary = review["descriptor_draft"]
	lines.append("")
	lines.append("Descriptor draft: anchor %s, footprint %s m, %d material slot(s), collision %s" % [
			draft["placement_anchor"], draft["footprint_radius_m"], (draft["material_slots"] as Array).size(),
			"yes" if draft["collision"] != null else "no"])
	lines.append("Nothing is published until you press Commit.")
	return "\n".join(lines)


static func _report_lines(report: Dictionary) -> PackedStringArray:
	var lines := PackedStringArray(["Portable conversion: %s" % report["portable_status"]])
	for o: String in report["omissions"]:
		lines.append("  omitted: %s" % o)
	for a: Dictionary in report["approximations"]:
		lines.append("  approximated (%s): %s" % [a["slot_id"], a["reason"]])
	return lines
