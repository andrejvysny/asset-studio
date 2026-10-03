@tool
extends RefCounted
# `godot_static_source_v1` half of the installer. Layout of a managed delivery directory:
#   source.zip      the original archive, byte-identical to the verified blob
#   source/...      the derived relocated tree (as_source_relocator.gd)
#   receipt.json    original manifest/file hashes and, separately, the hash of every installed (derived) file
#
# Receipt `source` object: {entry_scene, source_manifest_sha256, shader_trust, original_files: [{path, sha256,
# size}], installed_files: [{path (relative to the delivery dir), sha256, size, original_sha256, rewritten}]}.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")
const Policy = preload("res://addons/assetstudio/project/as_srcpkg_policy.gd")
const Package = preload("res://addons/assetstudio/project/as_source_package.gd")
const Relocator = preload("res://addons/assetstudio/project/as_source_relocator.gd")

const PORTABLE: String = "portable_glb_v1"


static func _unsupported(detail: String, message: String) -> RefCounted:
	return Result.fail("unsupported_source_dependency", message, false, {"detail": detail})


## Validates the archive, then writes source.zip and the derived tree into `staged` (an absolute, not yet existing
## directory). `opts`: {"trust_shaders": bool, "closure_keys": [asset_key], "managed_rel": String, "target_rel":
## String (project-relative final delivery dir)}. Nothing is
## written when validation or any pre-check fails. value = {"files": [{path, sha256, size}] (source.zip),
## "source": Dictionary (receipt `source` object)}.
static func stage(staged: String, prep: Dictionary, opts: Dictionary) -> RefCounted:
	var manifest: RefCounted = prep["manifest"]
	var entries: Array = manifest.data["files"]
	if entries.size() != 1 or entries[0]["path"] != Policy.ARCHIVE_NAME:
		return Result.fail("integrity_mismatch", "a source delivery must contain exactly %s" % Policy.ARCHIVE_NAME)
	var blob: String = prep["files"].get(Policy.ARCHIVE_NAME, "")
	if blob.is_empty() or FileAccess.get_sha256(blob) != entries[0]["sha256"]:
		return Result.fail("integrity_mismatch", "no verified blob for %s" % Policy.ARCHIVE_NAME)
	var pkg: RefCounted = Package.validate(blob)
	if not pkg.ok:
		return pkg
	var gate: RefCounted = _gate(pkg.value, manifest, opts)
	if not gate.ok:
		return gate
	var deps: RefCounted = _dependency_dirs(pkg.value, manifest, opts)
	if not deps.ok:
		return deps
	var delivery_res: String = "res://%s" % opts["target_rel"]
	var map: RefCounted = Relocator.build_map(pkg.value["manifest"], delivery_res, deps.value)
	if not map.ok:
		return map
	return _write(staged, blob, entries[0], pkg.value, map.value, bool(opts.get("trust_shaders", false)))


## Shader-bearing packages need the caller's explicit trust.
static func _gate(pkg: Dictionary, _manifest: RefCounted, opts: Dictionary) -> RefCounted:
	if pkg["shader_source"] and not bool(opts.get("trust_shaders", false)):
		return Result.fail("unsafe_package", "shader trust required: the package contains shader source", false,
				{"detail": "shader_trust_required"})
	return Result.success()


## asset_key -> "res://<managed>/<key>/<manifest sha>" of every dependency the package maps. Each must be in the
## lock closure and agree with the delivery manifest's pinned dependency list.
static func _dependency_dirs(pkg: Dictionary, manifest: RefCounted, opts: Dictionary) -> RefCounted:
	var closure: Array = opts.get("closure_keys", [])
	var pinned: Dictionary = {}
	for d: Dictionary in manifest.data["dependencies"]:
		pinned[d["asset_key"]] = d
	var zip_deps: Dictionary = pkg["manifest"]["asset_dependencies"]
	var out: Dictionary = {}
	for key: String in pkg["dependencies"]:
		if not closure.has(key) or not pinned.has(key):
			return _unsupported("dependency_not_in_closure", "dependency %s is not in the lock closure" % key.left(12))
		var z: Dictionary = zip_deps[key]
		var p: Dictionary = pinned[key]
		if z["descriptor_sha256"] != p["descriptor_sha256"] or z["representation"] != p["representation"] \
				or (z["delivery_id"] != null and z["delivery_id"] != p["delivery_id"]) or p["representation"] != PORTABLE:
			return _unsupported("dependency_mismatch", "dependency %s differs from the delivery manifest" % key.left(12))
		out[key] = "res://%s/%s/%s" % [opts["managed_rel"], key, p["manifest_sha256"]]
	return Result.success(out)


static func _write(staged: String, blob: String, archive: Dictionary, pkg: Dictionary, map: Dictionary,
		trusted: bool) -> RefCounted:
	var archive_dst: String = staged.path_join(Policy.ARCHIVE_NAME)
	if Fs.copy_file(blob, archive_dst) != OK or FileAccess.get_sha256(archive_dst) != archive["sha256"]:
		return Result.fail(Result.CODE_IO_ERROR, "cannot copy %s into staging" % Policy.ARCHIVE_NAME)
	var tree: RefCounted = Relocator.relocate(archive_dst, pkg["manifest"], staged.path_join(Relocator.TREE_DIR), map)
	if not tree.ok:
		return tree
	var installed: Array = []
	for f: Dictionary in tree.value:
		var item: Dictionary = f.duplicate()
		item["path"] = Relocator.TREE_DIR + "/" + f["path"]
		installed.append(item)
	var original: Array = []
	for f: Dictionary in pkg["manifest"]["files"]:
		original.append({"path": f["path"], "sha256": f["sha256"], "size": int(f["size"])})
	return Result.success({"files": [{"path": Policy.ARCHIVE_NAME, "sha256": archive["sha256"], "size": int(archive["size"])}],
			"source": {"entry_scene": pkg["entry_scene"], "source_manifest_sha256": pkg["manifest_sha256"],
			"shader_trust": trusted, "original_files": original, "installed_files": installed}})


## Receipt of an installed delivery -> "source/<entry_scene>" relative to the delivery dir, or "".
static func entry_rel(receipt: Dictionary) -> String:
	var src: Variant = receipt.get("source")
	if src is Dictionary and (src as Dictionary).get("entry_scene") is String:
		return "%s/%s" % [Relocator.TREE_DIR, src["entry_scene"]]
	return ""


## Problems of the derived tree of an installed delivery: every installed file must still match its recorded hash
## and the recorded original hashes must be the ones the package declared.
static func check_files(dir_abs: String, receipt: Dictionary) -> PackedStringArray:
	var problems := PackedStringArray()
	var src: Variant = receipt.get("source")
	if not src is Dictionary or not (src as Dictionary).get("installed_files") is Array \
			or not (src as Dictionary).get("original_files") is Array:
		problems.append("receipt has no source section")
		return problems
	for f: Variant in src["installed_files"]:
		if not f is Dictionary or not (f as Dictionary).has("path"):
			problems.append("receipt installed file entry malformed")
			continue
		var path: String = dir_abs.path_join(str(f["path"]))
		if not _matches(path, str(f.get("sha256")), int(f.get("size", -1))):
			problems.append("%s is missing or modified" % f["path"])
	return problems


static func _matches(path: String, sha: String, size: int) -> bool:
	var f: FileAccess = FileAccess.open(path, FileAccess.READ)
	if f == null:
		return false
	var ok: bool = f.get_length() == size
	f.close()
	return ok and FileAccess.get_sha256(path) == sha
