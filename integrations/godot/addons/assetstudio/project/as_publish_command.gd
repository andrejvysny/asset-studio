@tool
extends RefCounted
# `publish --scene res://x.tscn --library L [--new-version-of A --expected-current V] [--commit] ...` (AS-09).
# build (collect + package + portable GLB + descriptor draft + report) -> preview upload -> server validation shown
# -> explicit commit with compare-and-swap and lost-response recovery. Only .assetstudio/publish/ is written locally:
# the open scene, the project files and the lock are never touched, so a conflict leaves the local source as it was.
# prepare() and commit() are shared by the CLI (run) and the dock (as_publish_actions.gd). No watcher, no AI job.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")
const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")
const CJson = preload("res://addons/assetstudio/core/as_canonical_json.gd")
const Config = preload("res://addons/assetstudio/project/as_project_config.gd")
const Lock = preload("res://addons/assetstudio/project/as_project_lock.gd")
const Collector = preload("res://addons/assetstudio/project/as_source_collector.gd")
const Writer = preload("res://addons/assetstudio/project/as_source_writer.gd")
const Export = preload("res://addons/assetstudio/project/as_portable_export.gd")
const Journal = preload("res://addons/assetstudio/project/as_publish_journal.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")

const COMMIT_ATTEMPTS: int = 3
const RECOVERABLE: PackedStringArray = ["network_error", "timeout", "temporarily_unavailable"]


## CLI entry: prints the review JSON, then (with --commit) the outcome JSON. Exit 0 ok, 1 failure, 2 usage.
static func run(cmd: RefCounted, o: Dictionary) -> int:
	var prep: RefCounted = await prepare(cmd, o)
	if not prep.ok:
		return _fail(cmd, prep)
	cmd.say(JSON.stringify(prep.value["review"], "  ", true))
	if prep.value["done"] or not o.has("commit"):
		return 0
	var done: RefCounted = await commit(cmd, prep.value)
	if not done.ok:
		return _fail(cmd, done)
	cmd.say(JSON.stringify(done.value, "  ", true))
	return 0


static func _fail(cmd: RefCounted, r: RefCounted) -> int:
	if r.details.get("usage", false):
		cmd.err(r.message)
		return 2
	if r.code == "stale_pointer":
		var text: String = "conflict: the asset changed since it was read (current version %s). The local source is untouched. Re-run with --expected-current <current version> after review, or publish as a new asset (omit --new-version-of)."
		cmd.err(text % r.details.get("current_version_id", "unknown"))
		return 1
	if r.code == "temporarily_unavailable" and r.details.has("reason"):
		cmd.err("the server is busy (%s); retry later" % r.details["reason"])
	return cmd.fail_exit(r)


# --- prepare: build + preview --------------------------------------------------------------------------------

## value = {"review", "done", "intent_id", "entry", "receipt", "build", "options", "client", "library"}.
static func prepare(cmd: RefCounted, o: Dictionary) -> RefCounted:
	var opts: RefCounted = _options(o)
	if not opts.ok:
		return opts
	var cfg: RefCounted = Config.load_from(cmd.root)
	if not cfg.ok:
		return cfg
	var built: RefCounted = await _build(cmd, cfg.value, opts.value)
	if not built.ok:
		return built
	var id: String = _intent_id(opts.value, built.value)
	var entry: Dictionary = {} if o.has("fresh") else Journal.find(cmd.root, id)
	if entry.get("state", "") == "committed":
		return Result.success({"review": {"phase": "committed", "already_published": true, "outcome": entry["outcome"],
				"note": "this exact publication was already committed (journal); use --fresh to publish again"},
				"done": true, "outcome": entry["outcome"]})
	var client: Node = cmd.make_client(cfg.value.get("server_id"))
	var granted: RefCounted = await _check_scope(client)
	if not granted.ok:
		return granted
	var prep: Dictionary = {"intent_id": id, "entry": entry, "build": built.value, "options": opts.value, "client": client,
			"library": opts.value["library"], "cmd": cmd, "done": false, "receipt": {}}
	var previewed: RefCounted = await _ensure_preview(prep)
	if not previewed.ok:
		return previewed
	prep["review"] = _review(prep)
	return Result.success(prep)


static func _options(o: Dictionary) -> RefCounted:
	var usage: Dictionary = {"usage": true}
	if not Schema.matches("library_id", str(o.get("library", ""))):
		return Result.fail("invalid_request", "--library must be a library id (prj_...)", false, usage)
	if o.has("new-version-of") and (not Schema.matches("asset_id", str(o["new-version-of"])) or not Schema.matches("version_id", str(o.get("expected-current", "")))):
		return Result.fail("invalid_request", "--new-version-of needs an asset id and --expected-current a version id", false, usage)
	var scene: String = str(o["scene"])
	var tags: Array = []
	for t: String in str(o.get("tags", "")).split(",", false):
		tags.append(t.strip_edges())
	var name: String = str(o.get("name", scene.get_file().get_basename()))
	if name.is_empty() or name.length() > 200:
		return Result.fail("invalid_request", "--name must be 1..200 characters", false, usage)
	return Result.success({"scene": scene, "library": o["library"], "name": name, "tags": tags,
			"licence": str(o.get("licence", "unknown")), "category_id": o.get("category"),
			"target_asset_id": o.get("new-version-of"), "expected_current_version": o.get("expected-current"),
			"out": str(o.get("out", "")), "unsaved": o.get("unsaved", PackedStringArray())})


static func _check_scope(client: Node) -> RefCounted:
	var caps: RefCounted = await client.capabilities()
	if not caps.ok:
		return caps
	var granted: Variant = (caps.value.get("granted", {}) as Dictionary).get("scopes", [])
	if granted is Array and not (granted as Array).has("assets:publish"):
		return Result.fail("forbidden", "this credential cannot publish (scope assets:publish is missing)")
	return Result.success()


# --- build ---------------------------------------------------------------------------------------------------

static func _build(cmd: RefCounted, cfg: RefCounted, o: Dictionary) -> RefCounted:
	var lock: RefCounted = _load_lock(cmd.root)
	var collected: RefCounted = await Collector.collect(cmd.host(), cmd.root, o["scene"], {
			"managed_root": cfg.get("managed_root"), "lock": lock, "unsaved": o["unsaved"]})
	if not collected.ok:
		return collected
	var col: Dictionary = collected.value
	var dir: String = o["out"] if o["out"] != "" else cmd.root.path_join(".assetstudio/publish").path_join(o["scene"].get_file().get_basename())
	Fs.remove_tree(dir)
	DirAccess.make_dir_recursive_absolute(dir)
	var exported: RefCounted = Export.export_glb(col, dir.path_join("portable.glb"))
	Collector.release(col)
	if not exported.ok:
		return exported
	var report: Dictionary = Writer.conversion_report(exported.value)
	var package: RefCounted = Writer.write_package(col, report, dir.path_join("source.zip"))
	if not package.ok:
		return package
	return _documents(dir, col, exported.value, report, package.value)


static func _documents(dir: String, col: Dictionary, exported: Dictionary, report: Dictionary, package: Dictionary) -> RefCounted:
	var draft: Dictionary = Writer.descriptor_draft(col, exported)
	var draft_bytes: RefCounted = CJson.encode(draft)
	var report_bytes: RefCounted = CJson.encode(report)
	if not draft_bytes.ok or not report_bytes.ok:
		return Result.fail("invalid_request", "cannot encode the descriptor draft or report")
	Fs.write_atomic(dir.path_join("descriptor.json"), draft_bytes.value)
	Fs.write_atomic(dir.path_join("conversion_report.json"), report_bytes.value)
	var warnings: Array = col["warnings"]
	warnings.append_array(exported["warnings"])
	return Result.success({"dir": dir, "package": package, "portable": exported, "draft": draft,
			"draft_path": dir.path_join("descriptor.json"), "draft_sha256": Canonical.sha256_hex(draft_bytes.value),
			"report": report, "report_path": dir.path_join("conversion_report.json"), "warnings": warnings,
			"capabilities": col["capabilities"], "collision": col["collision"], "file_count": (col["files"] as Array).size(),
			"portable_facts": _facts(exported["facts"]),
			"asset_dependencies": col["asset_dependencies"]})


static func _facts(f: Dictionary) -> Dictionary:
	return {"triangles": f["triangles"], "nodes": f["nodes"], "materials": f["materials"], "max_texture_px": f["max_texture_px"], "bytes": f["bytes"]}


static func _load_lock(root: String) -> RefCounted:
	var path: String = root.path_join(Lock.FILE_NAME)
	if not FileAccess.file_exists(path):
		return null
	var parsed: RefCounted = Lock.parse_bytes(Fs.read_bytes(path))
	return parsed.value if parsed.ok else null


static func _intent_id(o: Dictionary, b: Dictionary) -> String:
	return Journal.intent_id({"library": o["library"], "scene": o["scene"], "package": b["package"]["sha256"],
			"portable": b["portable"]["sha256"], "draft": b["draft_sha256"], "target": o["target_asset_id"],
			"expected": o["expected_current_version"], "name": o["name"], "category": o["category_id"],
			"tags": o["tags"], "licence": o["licence"]})


# --- preview -------------------------------------------------------------------------------------------------

static func _ensure_preview(prep: Dictionary) -> RefCounted:
	var entry: Dictionary = prep["entry"]
	var stored: Variant = JSON.parse_string(str(entry["receipt_json"])) if entry.has("receipt_json") else null
	if entry.get("state", "") in ["previewed", "committing"] and stored is Dictionary:
		prep["receipt"] = stored  # reviewed before; a stale one is detected at commit (preview_expired)
		return Result.success()
	return await upload_preview(prep, true)


## Uploads the parts and records the receipt; `new_key` starts a fresh idempotency key.
static func upload_preview(prep: Dictionary, new_key: bool) -> RefCounted:
	var b: Dictionary = prep["build"]
	var parts: Dictionary = {
		"source": {"path": b["package"]["path"], "filename": "source.zip", "media_type": "application/zip"},
		"portable": {"path": b["portable"]["path"], "filename": "portable.glb", "media_type": "model/gltf-binary"},
		"descriptor": {"path": b["draft_path"], "filename": "descriptor.json", "media_type": "application/json"},
		"report": {"path": b["report_path"], "filename": "conversion_report.json", "media_type": "application/json"}}
	var r: RefCounted = await (prep["client"] as Node).publication_preview(prep["library"], parts)
	if not r.ok:
		return r
	var receipt: Dictionary = r.value
	if receipt.get("portable_sha256") != b["portable"]["sha256"] or receipt.get("package_sha256") != b["package"]["sha256"]:
		return Result.fail("integrity_mismatch", "the server received different bytes than were sent")
	var entry: Dictionary = prep["entry"]
	if new_key or not entry.has("idempotency_key"):
		entry["idempotency_key"] = Journal.new_key()
	entry.merge({"state": "previewed", "library_id": prep["library"], "scene": prep["options"]["scene"],
			"receipt_json": JSON.stringify(receipt), "target_asset_id": prep["options"]["target_asset_id"],
			"expected_current_version": prep["options"]["expected_current_version"]}, true)
	prep["entry"] = entry
	prep["receipt"] = receipt
	return _save(prep)


static func _save(prep: Dictionary) -> RefCounted:
	var root: String = (prep["cmd"] as RefCounted).get("root")
	if Journal.save(root, prep["intent_id"], prep["entry"]) != OK:
		return Result.fail(Result.CODE_IO_ERROR, "cannot write the publish journal")
	return Result.success()


## What the user reviews: server validation, local conversion report, descriptor draft and the commit target.
static func _review(prep: Dictionary) -> Dictionary:
	var b: Dictionary = prep["build"]
	var rc: Dictionary = prep["receipt"]
	var o: Dictionary = prep["options"]
	return {"phase": "previewed", "preview_id": rc.get("preview_id"), "expires_at": rc.get("expires_at"),
			"idempotency_key": prep["entry"].get("idempotency_key"),
			"target": {"library": o["library"], "target_asset_id": o["target_asset_id"], "expected_current_version": o["expected_current_version"],
					"mode": "new_version" if o["target_asset_id"] != null else "new_asset", "name": o["name"]},
			"server": {"warnings": rc.get("warnings", []), "budget": rc.get("budget"), "bounds": rc.get("bounds"), "source": rc.get("source")},
			"local_warnings": b["warnings"], "portable": b["portable_facts"], "conversion_report": b["report"], "descriptor_draft": b["draft"],
			"capabilities": b["capabilities"], "files": b["file_count"],
			"hashes": {"package_sha256": rc.get("package_sha256"), "portable_sha256": rc.get("portable_sha256"),
					"descriptor_draft_sha256": rc.get("descriptor_draft_sha256")}}


# --- commit --------------------------------------------------------------------------------------------------

## Explicit commit of a prepared (reviewed) preview. value = {"phase": "committed", "outcome": {...}}.
static func commit(cmd: RefCounted, prep: Dictionary) -> RefCounted:
	var client: Node = prep["client"]
	var entry: Dictionary = prep["entry"]
	if entry.get("state", "") == "committing":  # an earlier attempt may have reached the server
		var known: Dictionary = await _operation_outcome(client, prep["library"], entry["idempotency_key"])
		if not known.is_empty():
			return _committed(prep, known)
	entry["state"] = "committing"
	_save(prep)
	var last: RefCounted = null
	for attempt: int in COMMIT_ATTEMPTS:
		last = await client.publication_commit(prep["library"], _body(prep))
		if last.ok:
			return _committed(prep, _from_commit(prep["library"], last.value))
		if last.code == "preview_expired":
			return await _repreview(cmd, prep)
		if not RECOVERABLE.has(last.code):
			break
		var known: Dictionary = await _operation_outcome(client, prep["library"], entry["idempotency_key"])
		if not known.is_empty():
			return _committed(prep, known)
		await cmd.host().get_tree().create_timer(0.3 * pow(2.0, attempt)).timeout
	if not RECOVERABLE.has(last.code):
		entry["state"] = "previewed"
		_save(prep)
	return last


static func _body(prep: Dictionary) -> Dictionary:
	var o: Dictionary = prep["options"]
	var rc: Dictionary = prep["receipt"]
	return {"preview_id": rc["preview_id"], "target_asset_id": o["target_asset_id"],
			"expected_current_version": o["expected_current_version"], "package_sha256": rc["package_sha256"],
			"portable_sha256": rc["portable_sha256"], "descriptor_draft_sha256": rc["descriptor_draft_sha256"],
			"name": o["name"], "category_id": o["category_id"], "tags": o["tags"], "licence": o["licence"],
			"idempotency_key": prep["entry"]["idempotency_key"]}


## An expired preview is replaced by a new upload and a new key, unless the old key already committed.
static func _repreview(cmd: RefCounted, prep: Dictionary) -> RefCounted:
	var known: Dictionary = await _operation_outcome(prep["client"], prep["library"], prep["entry"]["idempotency_key"])
	if not known.is_empty():
		return _committed(prep, known)
	var up: RefCounted = await upload_preview(prep, true)
	if not up.ok:
		return up
	prep["review"] = _review(prep)
	var r: RefCounted = await (prep["client"] as Node).publication_commit(prep["library"], _body(prep))
	if not r.ok:
		return r
	return _committed(prep, _from_commit(prep["library"], r.value))


## {} when the operation is unknown or the query failed (the caller then retries with the same key).
static func _operation_outcome(client: Node, library: String, key: String) -> Dictionary:
	var q: RefCounted = await client.publication_operation(library, key)
	if q.ok and q.value.get("state") == "committed":
		return {"library_id": library, "asset_id": q.value["asset_id"], "version_id": q.value["version_id"],
				"display_version": q.value.get("display_version"), "via": "operation_query"}
	return {}


static func _from_commit(library: String, v: Dictionary) -> Dictionary:
	var ref: Dictionary = v.get("asset_ref", {})
	return {"library_id": library, "asset_id": ref.get("asset_id"), "version_id": ref.get("version_id"),
			"display_version": v.get("display_version"), "descriptor_sha256": v.get("descriptor_sha256"), "via": "commit"}


static func _committed(prep: Dictionary, outcome: Dictionary) -> RefCounted:
	prep["entry"]["state"] = "committed"
	prep["entry"]["outcome"] = outcome
	_save(prep)
	return Result.success({"phase": "committed", "outcome": outcome})
