extends SceneTree
# Used by run_source_tests.py (copied next to project.godot of a temp consumer project).
# Usage: godot --headless --path <project> --script res://source_install_probe.gd -- <contracts v1 dir>
# Installs every valid source package fixture straight from its zip (no server) in one coordinator transaction and
# writes installed.json {name: {"entry": "res://...", "dir": "res://..."}}.

const Coordinator = preload("res://addons/assetstudio/project/as_mutation_coordinator.gd")
const Installer = preload("res://addons/assetstudio/project/as_installer.gd")
const Descriptor = preload("res://addons/assetstudio/core/as_asset_descriptor.gd")
const Manifest = preload("res://addons/assetstudio/core/as_delivery_manifest.gd")
const CJson = preload("res://addons/assetstudio/core/as_canonical_json.gd")
const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")

const MANAGED: String = "assets/library"
## name, descriptor fixture, published delivery manifest fixture ("" = generated)
const PACKAGES: Array = [
	["primitive_prop", "primitive_prop", ""], ["primitive_prop_v2", "primitive_prop_v2", ""],
	["csg_hut", "csg_hut", ""], ["textured_tree", "textured_tree", "source_textured_tree"],
	["custom_shader_crystal", "custom_shader_crystal", ""], ["prop_cluster", "prop_cluster", "source_prop_cluster"],
	["vertex_color_rock_with_collision", "vertex_color_rock_with_collision", ""],
	["array_mesh_prop", "array_mesh_prop", ""],
]


func _init() -> void:
	var v1: String = OS.get_cmdline_user_args()[0]
	var root: String = ProjectSettings.globalize_path("res://").trim_suffix("/")
	var c: RefCounted = Coordinator.new(root)
	if not c.open("probe").ok:
		_die("cannot open the transaction")
	var installed: Dictionary = {}
	var keys: Array = []
	var dep: Dictionary = _prep(v1, "primitive_prop", "portable_primitive_prop", "", "portable.glb", v1.path_join("fixtures/glb/primitive_prop.portable.glb"))
	keys.append(dep["ref"].key())
	var r: RefCounted = Installer.install(c, MANAGED, dep["ref"], dep, {})
	if not r.ok:
		_die("portable dependency: " + r.describe())
	for p: Array in PACKAGES:
		var prep: Dictionary = _prep(v1, p[1], p[2], "godot_static_source_v1", "source.zip", v1.path_join("fixtures/source_packages/valid/%s.zip" % p[0]))
		keys.append(prep["ref"].key())
		var res: RefCounted = Installer.install(c, MANAGED, prep["ref"], prep, {"trust_shaders": true, "closure_keys": keys})
		if not res.ok:
			_die("%s: %s" % [p[0], res.describe()])
		installed[p[0]] = {"entry": "res://%s/%s" % [res.value["target_rel"], res.value["entry_rel"]], "dir": "res://" + res.value["target_rel"]}
	var done: RefCounted = c.commit()
	c.close()
	if not done.ok:
		_die("commit: " + done.describe())
	FileAccess.open("res://installed.json", FileAccess.WRITE).store_string(JSON.stringify(installed))
	print("INSTALLED %d" % installed.size())
	quit(0)


func _die(msg: String) -> void:
	printerr("PROBE_FAILED " + msg)
	quit(1)


func _prep(v1: String, descriptor: String, manifest_fixture: String, rep: String, file: String, blob: String) -> Dictionary:
	var desc_raw: PackedByteArray = FileAccess.get_file_as_bytes(v1.path_join("fixtures/descriptors/valid/%s.json" % descriptor))
	var desc: RefCounted = Descriptor.parse_bytes(desc_raw).value
	var bytes: PackedByteArray = FileAccess.get_file_as_bytes(blob)
	var man_raw: PackedByteArray
	if manifest_fixture != "":
		man_raw = FileAccess.get_file_as_bytes(v1.path_join("fixtures/manifests/valid/%s.json" % manifest_fixture))
	else:
		man_raw = CJson.encode({"schema_version": 1, "delivery_id": "dlv_00000000000000d5", "asset_ref": desc.asset_ref.to_dict(),
				"descriptor_sha256": desc.raw_sha256, "representation": rep, "profile_id": "godot-static-source",
				"profile_version": "1.0.0", "preparer": {"name": "probe", "version": "1.0.0"}, "entrypoint": file,
				"files": [{"path": file, "sha256": Canonical.sha256_hex(bytes), "size": bytes.size(),
				"media_type": "application/zip", "artifact_id": "art_00000000000000a1"}], "dependencies": [],
				"required_capabilities": []}).value
	var man: RefCounted = Manifest.parse_bytes(man_raw).value
	return {"descriptor": desc, "manifest": man, "files": {file: blob}, "delivery_id": man.data["delivery_id"], "ref": desc.asset_ref}
