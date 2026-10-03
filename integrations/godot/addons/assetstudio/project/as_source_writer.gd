@tool
extends RefCounted
# Writes GodotStaticSourcePackageV1 (contracts/godot-integration/v1/static-source-package.md): canonical
# source_manifest.json plus the closure files, as a deterministic ZIP (members sorted, fixed 1980-01-01 timestamps,
# regular-file attributes, deflate or stored). File bytes are copied unchanged: nothing is rewritten at publish.
# Also builds the descriptor draft and the conversion report that accompany the package in the preview upload.
#
# The ZIP is assembled here instead of with ZIPPacker because ZIPPacker stamps the current time. Deflate data and
# CRC-32 come from the engine's gzip codec (raw deflate stream = gzip body, CRC-32 = gzip trailer).

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")
const CJson = preload("res://addons/assetstudio/core/as_canonical_json.gd")
const Policy = preload("res://addons/assetstudio/project/as_srcpkg_policy.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")

const MANIFEST_NAME: String = "source_manifest.json"
const DOS_DATE_1980: int = 0x0021
const REGULAR_FILE_ATTR: int = 0x81A40000  # S_IFREG | 0644 in the high 16 bits


static func godot_version() -> String:
	var v: Dictionary = Engine.get_version_info()
	return "%d.%d.%d-%s" % [v["major"], v["minor"], v["patch"], v["status"]]


## Placement values shared by manifest and draft: collection["placement"] plus the defaults (overridable).
static func placement(collection: Dictionary, overrides: Dictionary = {}) -> Dictionary:
	var p: Dictionary = collection["placement"]
	var slots: Array = []
	for s: Dictionary in collection["slots"]:
		slots.append({"slot_id": s["slot_id"], "role": s["role"], "source_surfaces": s["source_surfaces"]})
	return {"placement_anchor": p["anchor"], "footprint_radius_m": overrides.get("footprint_radius_m", p["footprint_radius_m"]),
			"scale_range": overrides.get("scale_range", Policy.DEFAULT_SCALE_RANGE),
			"height_offset_range_m": overrides.get("height_offset_range_m", Policy.DEFAULT_HEIGHT_RANGE),
			"default_grounding": overrides.get("default_grounding", Policy.DEFAULT_GROUNDING), "material_slots": slots}


## {"portable_status", "omissions", "approximations"} from the export result.
static func conversion_report(exported: Dictionary) -> Dictionary:
	var approx: Array = exported["approximations"]
	return {"portable_status": "approximated" if not approx.is_empty() else "exact",
			"omissions": exported["omissions"], "approximations": approx}


static func manifest_doc(collection: Dictionary, files: Array, report: Dictionary, overrides: Dictionary = {}) -> Dictionary:
	return {"schema_version": 1, "entry_scene": collection["entry_scene"], "source_godot_version": godot_version(),
			"files": files, "resource_map": collection["resource_map"], "asset_dependencies": collection["asset_dependencies"],
			"capabilities": collection["capabilities"], "conversion_report": report,
			"placement": placement(collection, overrides)}


static func descriptor_draft(collection: Dictionary, exported: Dictionary, overrides: Dictionary = {}) -> Dictionary:
	var p: Dictionary = placement(collection, overrides)
	var slots: Array = []
	for s: Dictionary in exported["slots"]:
		slots.append({"slot_id": s["slot_id"], "role": s["role"], "surfaces": {
				"portable_glb_v1": s["portable"], "godot_static_source_v1": s["source_surfaces"]}})
	var warnings: Array = []
	if not (exported["approximations"] as Array).is_empty():
		warnings.append("custom_shader_approximated")
	if not (exported["warnings"] as Array).is_empty():
		warnings.append("portable_over_ipad_budget")
	return {"schema_version": 1, "placement_anchor": p["placement_anchor"], "footprint_radius_m": p["footprint_radius_m"],
			"scale_range": p["scale_range"], "height_offset_range_m": p["height_offset_range_m"],
			"default_grounding": p["default_grounding"], "material_slots": slots, "collision": collection["collision"],
			"preview_warnings": warnings}


## Writes the package. value = {"path", "sha256", "size", "manifest_sha256", "files": [{path, sha256, size, media_type}]}.
static func write_package(collection: Dictionary, report: Dictionary, out_path: String, overrides: Dictionary = {}) -> RefCounted:
	var members: Array = []
	var listed: Array = []
	for f: Dictionary in collection["files"]:
		var data: PackedByteArray = Fs.read_bytes(f["abs"])
		members.append({"name": f["path"], "data": data})
		listed.append({"path": f["path"], "sha256": Fs.sha256_bytes(data), "size": data.size(), "media_type": f["media_type"]})
	var checked: RefCounted = check_members(listed, collection["entry_scene"])
	if not checked.ok:
		return checked
	var encoded: RefCounted = CJson.encode(manifest_doc(collection, listed, report, overrides))
	if not encoded.ok:
		return encoded
	members.append({"name": MANIFEST_NAME, "data": encoded.value})
	var zipped: RefCounted = write_zip(members, out_path)
	if not zipped.ok:
		return zipped
	zipped.value["manifest_sha256"] = Fs.sha256_bytes(encoded.value)
	zipped.value["files"] = listed
	return zipped


## Grammar pre-check of the member list (static-source-package.md section 1 and 2); the server repeats it.
static func check_members(listed: Array, entry_scene: String) -> RefCounted:
	var folded: Dictionary = {}
	var total: int = 0
	var has_entry: bool = false
	if listed.is_empty() or listed.size() > Policy.MAX_FILES:
		return Result.fail("resource_limit", "a package holds 1..%d files" % Policy.MAX_FILES)
	for f: Dictionary in listed:
		var err: String = Schema.check_safe_path(f["path"], "path")
		if err != "" or f["path"] == MANIFEST_NAME or not Policy.ALLOWED_EXTENSIONS.has("." + str(f["path"]).get_extension()):
			return Result.fail("unsafe_package", "member %s is not a valid package path (%s)" % [f["path"], err])
		if folded.has(String(f["path"]).to_lower()):
			return Result.fail("unsafe_package", "members differ only by case: %s" % f["path"])
		folded[String(f["path"]).to_lower()] = true
		has_entry = has_entry or f["path"] == entry_scene
		total += int(f["size"])
	if not has_entry:
		return Result.fail("unsafe_package", "the entry scene is not a package file")
	if total > Policy.MAX_EXPANDED_BYTES:
		return Result.fail("resource_limit", "the package expands to more than %d bytes" % Policy.MAX_EXPANDED_BYTES)
	return Result.success()


# --- ZIP -----------------------------------------------------------------------------------------------------

## members: [{"name": String, "data": PackedByteArray}]. value = {"path", "sha256", "size"}.
static func write_zip(members: Array, out_path: String) -> RefCounted:
	var sorted_members: Array = members.duplicate()
	sorted_members.sort_custom(func(a: Dictionary, b: Dictionary) -> bool: return CJson.key_less(a["name"], b["name"]))
	DirAccess.make_dir_recursive_absolute(out_path.get_base_dir())
	var f: FileAccess = FileAccess.open(out_path, FileAccess.WRITE)
	if f == null:
		return Result.fail(Result.CODE_IO_ERROR, "cannot write %s" % out_path)
	var central := PackedByteArray()
	for m: Dictionary in sorted_members:
		var entry: RefCounted = _entry(m["data"])
		if not entry.ok:
			f.close()
			return entry
		central.append_array(_central_record(m["name"], entry.value, f.get_position()))
		_local_record(f, m["name"], entry.value)
	var cd_offset: int = f.get_position()
	f.store_buffer(central)
	f.store_32(0x06054B50)
	f.store_16(0)
	f.store_16(0)
	f.store_16(sorted_members.size())
	f.store_16(sorted_members.size())
	f.store_32(central.size())
	f.store_32(cd_offset)
	f.store_16(0)
	f.close()
	var size: int = FileAccess.open(out_path, FileAccess.READ).get_length()
	if size > Policy.MAX_UPLOAD_BYTES:
		return Result.fail("resource_limit", "the package is larger than the %d byte upload limit" % Policy.MAX_UPLOAD_BYTES)
	return Result.success({"path": out_path, "sha256": FileAccess.get_sha256(out_path), "size": size})


## {"crc", "method", "payload", "size"}: deflated when that is smaller and keeps the ratio under the server cap.
static func _entry(data: PackedByteArray) -> RefCounted:
	if data.is_empty():
		return Result.success({"crc": 0, "method": 0, "payload": data, "size": 0})
	var gz: PackedByteArray = data.compress(FileAccess.COMPRESSION_GZIP)
	if gz.size() < 19 or gz[0] != 0x1F or gz[1] != 0x8B or gz[2] != 8 or gz[3] != 0:
		return Result.fail(Result.CODE_IO_ERROR, "unexpected gzip stream from the engine")
	var raw: PackedByteArray = gz.slice(10, gz.size() - 8)
	var deflate: bool = raw.size() < data.size() and data.size() <= raw.size() * Policy.RATIO_LIMIT
	return Result.success({"crc": gz.decode_u32(gz.size() - 8), "method": 8 if deflate else 0,
			"payload": raw if deflate else data, "size": data.size()})


static func _local_record(f: FileAccess, name: String, e: Dictionary) -> void:
	var bytes: PackedByteArray = name.to_utf8_buffer()
	f.store_32(0x04034B50)
	f.store_16(20)
	f.store_16(0)
	f.store_16(e["method"])
	f.store_16(0)
	f.store_16(DOS_DATE_1980)
	f.store_32(e["crc"])
	f.store_32((e["payload"] as PackedByteArray).size())
	f.store_32(e["size"])
	f.store_16(bytes.size())
	f.store_16(0)
	f.store_buffer(bytes)
	f.store_buffer(e["payload"])


static func _central_record(name: String, e: Dictionary, offset: int) -> PackedByteArray:
	var bytes: PackedByteArray = name.to_utf8_buffer()
	var rec := StreamPeerBuffer.new()
	rec.put_u32(0x02014B50)
	rec.put_u16((3 << 8) | 20)
	rec.put_u16(20)
	rec.put_u16(0)
	rec.put_u16(e["method"])
	rec.put_u16(0)
	rec.put_u16(DOS_DATE_1980)
	rec.put_u32(e["crc"])
	rec.put_u32((e["payload"] as PackedByteArray).size())
	rec.put_u32(e["size"])
	rec.put_u16(bytes.size())
	rec.put_u16(0)
	rec.put_u16(0)
	rec.put_u16(0)
	rec.put_u16(0)
	rec.put_u32(REGULAR_FILE_ATTR)
	rec.put_u32(offset)
	rec.put_data(bytes)
	return rec.data_array
