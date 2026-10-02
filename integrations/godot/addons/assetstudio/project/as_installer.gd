@tool
extends RefCounted
# Materializes a verified delivery into <managed_root>/<asset_key>/<manifest_sha256>/ (design §4.2, §4.3).
# Bytes come from the verified blob cache and are COPIED (never hardlinked). Everything is staged under
# <managed_root>/.staging/<txn> and renamed into place by the coordinator transaction. An existing target
# is verified and never overwritten.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const CJson = preload("res://addons/assetstudio/core/as_canonical_json.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")

const INSTALLER_VERSION: String = "1.0.0"
const RECEIPT_NAME: String = "receipt.json"
const IMPORT_PARAMS: PackedStringArray = [
	"array_mesh/deduplicate_surfaces=false",  # glTF primitive p stays surface p
	"meshes/generate_lods=true",
	"materials/extract=0",
	"nodes/apply_root_scale=true",
	"_subresources={}",
]


static func target_rel(managed_rel: String, asset_key: String, manifest_sha256: String) -> String:
	return managed_rel.path_join(asset_key).path_join(manifest_sha256)


## `prep` is the value of ASAssetResolver.prepare(). On success value = {"status": "installed" | "present",
## "target_rel": String}; "installed" means a directory rename was queued on `coord` (nothing is on disk yet).
static func install(coord: RefCounted, managed_rel: String, ref: RefCounted, prep: Dictionary) -> RefCounted:
	var key: String = ref.call("key")
	var manifest: RefCounted = prep["manifest"]
	var msha: String = manifest.get("raw_sha256")
	var rel: String = target_rel(managed_rel, key, msha)
	var expect: Dictionary = {"asset_key": key, "manifest_sha256": msha, "delivery_id": prep["delivery_id"],
			"representation": manifest.data["representation"]}
	if Fs.exists(coord.call("abs_path", rel)):
		var problems: PackedStringArray = check_install(coord.call("abs_path", rel), expect)
		if not problems.is_empty():
			return Result.fail("integrity_mismatch", "installed delivery is modified: %s" % problems[0],
					false, {"target": rel, "problems": Array(problems)})
		return Result.success({"status": "present", "target_rel": rel})
	var staged_rel: String = (coord.call("new_staging_dir", managed_rel) as String).path_join("%s-%s" % [key, msha])
	var staged: String = coord.call("abs_path", staged_rel)
	var files: RefCounted = _copy_files(staged, manifest.data["files"], prep["files"])
	if not files.ok:
		return files
	var receipt: RefCounted = _receipt_bytes(ref, prep, files.value)
	if not receipt.ok:
		return receipt
	if Fs.write_atomic(staged.path_join(RECEIPT_NAME), receipt.value) != OK:
		return Result.fail(Result.CODE_IO_ERROR, "cannot write receipt")
	coord.call("add_dir", staged_rel, rel, Fs.sha256_bytes(receipt.value))
	return Result.success({"status": "installed", "target_rel": rel})


## Copies each manifest file out of its blob and verifies size and sha256 of the copy.
## value = [{"path", "sha256", "size"}] in manifest order.
static func _copy_files(staged: String, entries: Array, blobs: Dictionary) -> RefCounted:
	var out: Array = []
	for f: Dictionary in entries:
		var path: String = f["path"]
		if not blobs.has(path):
			return Result.fail("integrity_mismatch", "no verified blob for %s" % path)
		var dst: String = staged.path_join(path)
		if Fs.copy_file(blobs[path], dst) != OK:
			return Result.fail(Result.CODE_IO_ERROR, "cannot copy %s into staging" % path)
		if not _matches(dst, f["sha256"], int(f["size"])):
			return Result.fail("integrity_mismatch", "copied bytes of %s do not match the manifest" % path)
		if path.get_extension().to_lower() == "glb":
			var cfg: String = "[params]\n\n%s\n" % "\n".join(IMPORT_PARAMS)
			if Fs.write_atomic(dst + ".import", cfg.to_utf8_buffer()) != OK:
				return Result.fail(Result.CODE_IO_ERROR, "cannot write import settings")
		out.append({"path": path, "sha256": f["sha256"], "size": int(f["size"])})
	return Result.success(out)


static func _receipt_bytes(ref: RefCounted, prep: Dictionary, files: Array) -> RefCounted:
	return CJson.encode({"schema_version": 1, "asset_key": ref.call("key"), "asset_ref": ref.call("to_dict"),
			"delivery_id": prep["delivery_id"], "representation": prep["manifest"].data["representation"],
			"manifest_sha256": prep["manifest"].get("raw_sha256"),
			"descriptor_sha256": prep["descriptor"].get("raw_sha256"), "files": files,
			"installer_version": INSTALLER_VERSION})


static func _matches(path: String, sha: String, size: int) -> bool:
	var f: FileAccess = FileAccess.open(path, FileAccess.READ)
	if f == null:
		return false
	var ok: bool = f.get_length() == size
	f.close()
	return ok and FileAccess.get_sha256(path) == sha


## Every problem found in an installed directory (empty = intact). `expect` = {asset_key, manifest_sha256,
## delivery_id, representation}. Extra files (Godot's .import, caches) are ignored.
static func check_install(dir_abs: String, expect: Dictionary) -> PackedStringArray:
	var problems := PackedStringArray()
	var parsed: RefCounted = CJson.parse_canonical(Fs.read_bytes(dir_abs.path_join(RECEIPT_NAME)))
	if not parsed.ok or not parsed.value is Dictionary:
		problems.append("%s missing or not canonical" % RECEIPT_NAME)
		return problems
	var rc: Dictionary = parsed.value
	for k: String in expect:
		if rc.get(k) != expect[k]:
			problems.append("receipt %s differs from the locked value" % k)
	if not rc.get("files") is Array:
		problems.append("receipt has no file list")
		return problems
	for f: Variant in rc["files"]:
		if not f is Dictionary or not (f as Dictionary).has("path"):
			problems.append("receipt file entry malformed")
			continue
		if not _matches(dir_abs.path_join(str(f["path"])), str(f.get("sha256")), int(f.get("size", -1))):
			problems.append("%s is missing or modified" % f["path"])
	return problems


## Evidence that Godot (or the installer pre-seed) knows the file: every .glb has a sibling .import.
static func missing_import_files(dir_abs: String) -> PackedStringArray:
	var out := PackedStringArray()
	var parsed: RefCounted = CJson.parse_strict_utf8(Fs.read_bytes(dir_abs.path_join(RECEIPT_NAME)))
	if not parsed.ok or not parsed.value is Dictionary:
		return out
	for f: Variant in (parsed.value as Dictionary).get("files", []):
		if f is Dictionary and str(f.get("path", "")).get_extension().to_lower() == "glb":
			if not FileAccess.file_exists(dir_abs.path_join(str(f["path"])) + ".import"):
				out.append(str(f["path"]))
	return out
