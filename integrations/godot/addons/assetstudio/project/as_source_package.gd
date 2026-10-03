@tool
extends RefCounted
# Client-side static validator of a `godot_static_source_v1` archive (static-source-package.md §1-§5). Reads the
# archive bytes only; nothing is written, loaded or executed. The first violation is returned with the same
# code/detail as the server validator (fixtures/INDEX.json). Everything runs BEFORE the installer touches the
# project.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")
const Zip = preload("res://addons/assetstudio/project/as_srcpkg_zip.gd")
const Policy = preload("res://addons/assetstudio/project/as_srcpkg_policy.gd")
const Manifest = preload("res://addons/assetstudio/project/as_srcpkg_manifest.gd")
const Media = preload("res://addons/assetstudio/project/as_srcpkg_media.gd")
const Rules = preload("res://addons/assetstudio/project/as_srcpkg_rules.gd")

const SHADER_SUFFIXES: PackedStringArray = [".gdshader", ".gdshaderinc"]
const IMAGE_SUFFIXES: PackedStringArray = [".png", ".jpg", ".jpeg", ".webp"]


## value = {"manifest": Dictionary, "manifest_sha256", "entry_scene", "dependencies": [asset_key],
## "shader_source": bool, "detected": [capability]}. `path` is an absolute filesystem path of the zip.
static func validate(path: String) -> RefCounted:
	var opened: RefCounted = Zip.open(path)
	if not opened.ok:
		return opened
	var zip: RefCounted = opened.value
	var r: RefCounted = _validate(zip)
	zip.call("close")
	return r


static func _validate(zip: RefCounted) -> RefCounted:
	var members: Dictionary = zip.get("members")
	if not members.has(Policy.MANIFEST_NAME):
		return Zip.unsafe("manifest_missing", "%s is missing" % Policy.MANIFEST_NAME, Policy.MANIFEST_NAME)
	if int(members[Policy.MANIFEST_NAME]["size"]) > Policy.MAX_MANIFEST_BYTES:
		return Zip.limit("manifest_size", "%s larger than 8 MiB" % Policy.MANIFEST_NAME, Policy.MANIFEST_NAME)
	var raw: RefCounted = zip.call("read", Policy.MANIFEST_NAME)
	if not raw.ok:
		return raw
	var parsed: RefCounted = Manifest.parse(raw.value)
	if not parsed.ok:
		return parsed
	var manifest: Dictionary = parsed.value
	var steps: Array[Callable] = [_check_member_set.bind(members, manifest), _check_hashes.bind(zip, manifest)]
	for step: Callable in steps:
		var s: RefCounted = step.call()
		if not s.ok:
			return s
	var rules: RefCounted = Rules.new(manifest)
	var checked: RefCounted = _check_contents(zip, manifest, rules)
	if not checked.ok:
		return checked
	var facts: RefCounted = rules.call("finish")
	if not facts.ok:
		return facts
	var out: Dictionary = facts.value
	out["manifest"] = manifest
	out["manifest_sha256"] = Canonical.sha256_hex(raw.value)
	out["entry_scene"] = manifest["entry_scene"]
	return Result.success(out)


static func _check_member_set(members: Dictionary, manifest: Dictionary) -> RefCounted:
	var declared: Dictionary = {}
	for f: Dictionary in manifest["files"]:
		declared[f["path"]] = true
	var names: Array = members.keys()
	names.sort()
	for name: String in names:
		if name != Policy.MANIFEST_NAME and not declared.has(name):
			return Zip.unsafe("undeclared_member", "member is not listed in source_manifest.json", name)
	var paths: Array = declared.keys()
	paths.sort()
	for p: String in paths:
		if not members.has(p):
			return Zip.unsafe("missing_member", "declared file is not in the archive", p)
	for p: String in paths:
		var problem: Array = extension_problem(p)
		if not problem.is_empty():
			return Zip.unsafe(problem[0], problem[1], p)
	return Result.success()


## [detail, message] or [] when the extension policy accepts `name`.
static func extension_problem(name: String) -> Array:
	var base: String = name.get_file().to_lower()
	var lower: String = name.to_lower()
	for entry: String in Policy.FORBIDDEN_EXTENSIONS:
		if entry.begins_with(".") and lower.ends_with(entry):
			var detail: String = "binary_resource" if entry == ".scn" or entry == ".res" else "forbidden_extension"
			return [detail, "%s members are forbidden" % entry]
		if not entry.begins_with(".") and base == entry:
			return ["forbidden_file", "%s members are forbidden" % entry]
	if not Policy.ALLOWED_EXTENSIONS.has(suffix_of(name)):
		return ["extension_not_allowed", "file extension is not allowed"]
	return []


static func suffix_of(path: String) -> String:
	var base: String = path.get_file()
	var i: int = base.rfind(".")
	return base.substr(i) if i > 0 else ""


## Declared size and sha256 of every member, in manifest order (integrity_mismatch).
static func _check_hashes(zip: RefCounted, manifest: Dictionary) -> RefCounted:
	for f: Dictionary in manifest["files"]:
		var data: RefCounted = zip.call("read", f["path"])
		if not data.ok:
			return data
		var bytes: PackedByteArray = data.value
		if bytes.size() != int(f["size"]):
			return Result.fail("integrity_mismatch", "size %d differs from declared %d" % [bytes.size(), int(f["size"])],
					false, {"detail": "size", "path": f["path"]})
		if Canonical.sha256_hex(bytes) != f["sha256"]:
			return Result.fail("integrity_mismatch", "SHA-256 differs from the declared hash", false,
					{"detail": "sha256", "path": f["path"]})
	return Result.success()


## Content rules per member in path order (the server's order); images and GLBs are header-checked.
static func _check_contents(zip: RefCounted, manifest: Dictionary, rules: RefCounted) -> RefCounted:
	var paths: Array = []
	for f: Dictionary in manifest["files"]:
		paths.append(f["path"])
	paths.sort()
	for p: String in paths:
		var suffix: String = suffix_of(p)
		if suffix != ".tscn" and suffix != ".tres" and not SHADER_SUFFIXES.has(suffix) and not IMAGE_SUFFIXES.has(suffix) and suffix != ".glb":
			continue
		var data: RefCounted = zip.call("read", p)
		if not data.ok:
			return data
		var r: RefCounted
		if suffix == ".tscn" or suffix == ".tres":
			r = rules.call("check_text", p, data.value, suffix)
		elif SHADER_SUFFIXES.has(suffix):
			r = rules.call("check_shader", p, data.value)
		elif suffix == ".glb":
			r = Media.check_glb(p, data.value)
		else:
			r = Media.check_image(p, data.value, suffix)
		if not r.ok:
			return r
	return Result.success()
