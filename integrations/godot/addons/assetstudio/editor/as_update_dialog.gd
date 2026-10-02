@tool
extends ConfirmationDialog
# Update review (design §8): current vs target exact version, descriptor differences, affected instances.
# Buttons: "Update binding" (confirmed), "Update selected instances" (custom action), "Dismiss" (canceled).
# The dialog only reports the choice; as_dock_actions.gd applies it.

signal choice(mode: String)  # "binding" | "instances" | "dismiss"

var _body: RichTextLabel = null


func _init() -> void:
	title = "AssetStudio update"
	ok_button_text = "Update binding"
	cancel_button_text = "Dismiss"
	add_button("Update selected instances", false, "instances")
	_body = RichTextLabel.new()
	_body.custom_minimum_size = Vector2(520, 260)
	_body.bbcode_enabled = false
	add_child(_body)
	confirmed.connect(func() -> void: choice.emit("binding"))
	canceled.connect(func() -> void: choice.emit("dismiss"))
	custom_action.connect(func(action: StringName) -> void:
		choice.emit(str(action))
		hide())


## `review` = ASDockActions.review() value.
func show_review(review: Dictionary) -> void:
	_body.text = summary_text(review)
	popup_centered()


static func summary_text(review: Dictionary) -> String:
	var lines: PackedStringArray = []
	lines.append("Binding: %s" % review["binding_id"])
	lines.append("Current version: %s" % review["from"])
	lines.append("Target version:  %s" % review["to"])
	lines.append("Instances in the open scene: %d" % int(review["instances"]))
	lines.append("")
	if (review["diff"] as PackedStringArray).is_empty():
		lines.append("No descriptor differences (anchor, bounds, scale, slots, collision).")
	else:
		lines.append("Descriptor differences:")
		for d: String in review["diff"]:
			lines.append("  - %s" % d)
	lines.append("")
	lines.append("Update binding: all instances follow the new version.")
	lines.append("Update selected instances: only the selected instances move to a new binding.")
	return "\n".join(lines)
