extends "res://tests/project_test_base.gd"
# Helpers (not a test file) for the godot_static_source_v1 tests: preps built directly from the fixture zips.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Coordinator = preload("res://addons/assetstudio/project/as_mutation_coordinator.gd")
const Installer = preload("res://addons/assetstudio/project/as_installer.gd")
const Descriptor = preload("res://addons/assetstudio/core/as_asset_descriptor.gd")
const Manifest = preload("res://addons/assetstudio/core/as_delivery_manifest.gd")
const BlobCache = preload("res://addons/assetstudio/core/as_blob_cache.gd")
const Lock = preload("res://addons/assetstudio/project/as_project_lock.gd")

const MANAGED: String = "assets/library"
const SOURCE_REP: String = "godot_static_source_v1"
const PORTABLE_REP: String = "portable_glb_v1"


func zip_file(kind: String, name: String) -> String:
	return contracts_dir().path_join("fixtures/source_packages").path_join(kind).path_join("%s.zip" % name)


## Delivery manifest of a source zip (the published fixture manifest when `manifest_fixture` is given).
func source_prep(zip_abs: String, descriptor_fixture: String, manifest_fixture: String = "",
		dependencies: Array = []) -> Dictionary:
	var desc_raw: PackedByteArray = fixture("descriptors/valid/%s.json" % descriptor_fixture)
	var desc: RefCounted = Descriptor.parse_bytes(desc_raw).value
	var bytes: PackedByteArray = Fs.read_bytes(zip_abs)
	var man_raw: PackedByteArray
	if manifest_fixture != "":
		man_raw = fixture("manifests/valid/%s.json" % manifest_fixture)
	else:
		var doc: Dictionary = {"schema_version": 1, "delivery_id": "dlv_00000000000000d5", "asset_ref": desc.asset_ref.to_dict(),
				"descriptor_sha256": desc.raw_sha256, "representation": SOURCE_REP, "profile_id": "godot-static-source",
				"profile_version": "1.0.0", "preparer": {"name": "test", "version": "1.0.0"}, "entrypoint": "source.zip",
				"files": [{"path": "source.zip", "sha256": Fs.sha256_bytes(bytes), "size": bytes.size(),
				"media_type": "application/zip", "artifact_id": "art_00000000000000a1"}], "dependencies": dependencies,
				"required_capabilities": []}
		man_raw = CJson.encode(doc).value
	var man: RefCounted = Manifest.parse_bytes(man_raw).value
	return {"descriptor": desc, "descriptor_raw": desc_raw, "manifest": man, "manifest_raw": man_raw,
			"files": {"source.zip": zip_abs}, "delivery_id": man.data["delivery_id"], "ref": desc.asset_ref}


## Portable delivery of primitive_prop v1 (the dependency of prop_cluster); the glb sits in a temp dir.
func portable_prep() -> Dictionary:
	var desc_raw: PackedByteArray = fixture("descriptors/valid/primitive_prop.json")
	var man_raw: PackedByteArray = fixture("manifests/valid/portable_primitive_prop.json")
	var desc: RefCounted = Descriptor.parse_bytes(desc_raw).value
	var man: RefCounted = Manifest.parse_bytes(man_raw).value
	var glb: String = tmp_dir("blob").path_join("portable.glb")
	Fs.write_atomic(glb, fixture("glb/primitive_prop.portable.glb"))
	return {"descriptor": desc, "descriptor_raw": desc_raw, "manifest": man, "manifest_raw": man_raw,
			"files": {"portable.glb": glb}, "delivery_id": man.data["delivery_id"], "ref": desc.asset_ref}


func delivery_dir(root: String, prep: Dictionary) -> String:
	return root.path_join(Installer.target_rel(MANAGED, prep["ref"].key(), prep["manifest"].raw_sha256))


## Installs `preps` ([{prep, opts}]) in one transaction. Returns the commit result (or the first failure).
func install_all(root: String, items: Array) -> RefCounted:
	var c: RefCounted = Coordinator.new(root)
	var opened: RefCounted = c.open("install")
	assert_true(opened.ok, "open: %s" % opened.describe())
	for it: Dictionary in items:
		var r: RefCounted = Installer.install(c, MANAGED, it["prep"]["ref"], it["prep"], it.get("opts", {}))
		if not r.ok:
			c.close()
			return r
	var done: RefCounted = c.commit()
	c.close()
	return done


func read_json(path: String) -> Variant:
	return JSON.parse_string(read_text(path))


## Blob cache holding the delivery of `prep` the way the resolver leaves it after a download.
func cache_delivery(cache: RefCounted, prep: Dictionary, rep: String) -> void:
	var man: RefCounted = prep["manifest"]
	cache.store_document("descriptors", prep["descriptor"].raw_sha256, prep["descriptor_raw"])
	cache.store_document("manifests", man.raw_sha256, prep["manifest_raw"])
	var f: Dictionary = man.data["files"][0]
	var stage: String = ProjectSettings.globalize_path(cache.staging_path(f["sha256"]))
	Fs.write_atomic(stage, Fs.read_bytes(prep["files"][f["path"]]))
	var inst: RefCounted = cache.install_from_staging(cache.staging_path(f["sha256"]), f["sha256"], int(f["size"]))
	assert_true(inst.ok, "blob install: %s" % inst.describe())
	cache.write_ref_index(prep["ref"].key(), {"asset_key": prep["ref"].key(), "entries": {rep: {
			"descriptor_sha256": prep["descriptor"].raw_sha256, "delivery_id": man.data["delivery_id"],
			"manifest_sha256": man.raw_sha256}}})


func lock_dependency(lock: RefCounted, prep: Dictionary, requires: Array) -> void:
	var m: RefCounted = prep["manifest"]
	var delivery: Dictionary = {"delivery_id": prep["delivery_id"], "manifest_sha256": m.raw_sha256,
			"profile_id": m.data["profile_id"], "profile_version": m.data["profile_version"]}
	assert_eq(lock.add_dependency(prep["ref"], prep["descriptor"].raw_sha256, m.data["representation"], delivery, requires), "", "dep")
