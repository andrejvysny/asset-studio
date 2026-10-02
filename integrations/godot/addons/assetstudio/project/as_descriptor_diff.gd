@tool
extends RefCounted
# Human-readable differences between two descriptors for the update review (design §8): anchor, bounds,
# footprint, scale and height ranges, grounding, collision and material slots (added, removed, role changed).

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Descriptor = preload("res://addons/assetstudio/core/as_asset_descriptor.gd")
const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")

const SCALAR_FIELDS: PackedStringArray = ["placement_anchor", "bounds_min", "bounds_max", "footprint_radius_m",
		"scale_range", "height_offset_range_m", "default_grounding"]


## `old_d` / `new_d` = descriptor `data` dictionaries. Empty array = no relevant difference.
static func diff(old_d: Dictionary, new_d: Dictionary) -> PackedStringArray:
	var out := PackedStringArray()
	for f: String in SCALAR_FIELDS:
		if old_d[f] != new_d[f]:
			out.append("%s: %s -> %s" % [f, _fmt(old_d[f]), _fmt(new_d[f])])
	if old_d["collision"] != new_d["collision"]:
		out.append("collision: %s -> %s" % [_fmt(old_d["collision"]), _fmt(new_d["collision"])])
	out.append_array(_slot_diff(old_d["material_slots"], new_d["material_slots"]))
	return out


static func _fmt(v: Variant) -> String:
	if v == null:
		return "none"
	if v is Array:
		return "[%s]" % ", ".join(PackedStringArray((v as Array).map(func(x: Variant) -> String: return str(x))))
	if v is Dictionary:
		return JSON.stringify(v, "", true)
	return str(v)


static func _slot_diff(old_slots: Array, new_slots: Array) -> PackedStringArray:
	var out := PackedStringArray()
	var old_by: Dictionary = {}
	for s: Dictionary in old_slots:
		old_by[s["slot_id"]] = s
	var new_by: Dictionary = {}
	for s: Dictionary in new_slots:
		new_by[s["slot_id"]] = s
	for id: String in old_by:
		if not new_by.has(id):
			out.append("slot removed: %s (%s)" % [id, old_by[id]["role"]])
		elif old_by[id]["role"] != new_by[id]["role"]:
			out.append("slot %s role: %s -> %s" % [id, old_by[id]["role"], new_by[id]["role"]])
	for id: String in new_by:
		if not old_by.has(id):
			out.append("slot added: %s (%s)" % [id, new_by[id]["role"]])
	return out


## Descriptor of an exact version through the resolve route (no download of the delivery files).
## value = ASAssetDescriptor. The sha256 the server states must match the bytes.
static func fetch_descriptor(client: Node, ref: RefCounted) -> RefCounted:
	var res: RefCounted = await client.resolve(ref.get("library_id"), [ref])
	if not res.ok:
		return res
	var entries: Array = res.value["entries"]
	if entries.size() != 1 or not entries[0] is Dictionary or entries[0].get("state") != "ready" \
			or not entries[0].get("descriptor_json") is String:
		return Result.fail("version_unavailable", "the target descriptor is not available yet")
	var raw: PackedByteArray = (entries[0]["descriptor_json"] as String).to_utf8_buffer()
	if Canonical.sha256_hex(raw) != entries[0].get("descriptor_sha256"):
		return Result.fail("integrity_mismatch", "descriptor bytes do not match their sha256")
	return Descriptor.parse_bytes(raw)
