extends Node
# Exact-reference resolver: AssetRef -> verified descriptor + manifest + local blob paths.
# Never substitutes another or the "latest" version: when the exact bytes cannot be obtained or found in the
# cache the result is an error. Downloads files into the ASBlobCache only; creating scenes/textures from them
# is the consumer's job (do it on the main thread when no editing gesture is active).
#
# States emitted via state_changed(asset_key, state, progress): remote, downloading, verified, failed, cancelled.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")
const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")
const AssetRef = preload("res://addons/assetstudio/core/as_asset_ref.gd")
const Descriptor = preload("res://addons/assetstudio/core/as_asset_descriptor.gd")
const Manifest = preload("res://addons/assetstudio/core/as_delivery_manifest.gd")

signal state_changed(asset_key: String, state: String, progress: float)

const STATE_REMOTE: String = "remote"
const STATE_DOWNLOADING: String = "downloading"
const STATE_VERIFIED: String = "verified"
const STATE_FAILED: String = "failed"
const STATE_CANCELLED: String = "cancelled"

## When true the network is never touched.
var offline_only: bool = false

var _client: Node = null
var _cache: RefCounted = null


func setup(client: Node, cache: RefCounted) -> void:
	_client = client
	_cache = cache


## value = {"descriptor": ASAssetDescriptor, "manifest": ASDeliveryManifest, "files": {path: blob path},
## "delivery_id": String, "source": "network" | "cache"}.
## `pin_delivery_id` (restore): only that exact delivery is acceptable; a server that does not offer it, or a cache
## holding another one, is an error and never a silent substitution.
func prepare(ref: RefCounted, representation: String = "portable_glb_v1", cancel_token: RefCounted = null,
		pin_delivery_id: String = "") -> RefCounted:
	var key: String = ref.call("key")
	if not Schema.REPRESENTATIONS.has(representation):
		return _finish(key, Result.fail("invalid_request", "unknown representation"))
	state_changed.emit(key, STATE_REMOTE, 0.0)
	if offline_only:
		return _finish(key, _from_cache(ref, representation, true, pin_delivery_id))
	var r: RefCounted = await _from_network(ref, representation, cancel_token, pin_delivery_id)
	if not r.ok and r.code in [Result.CODE_NETWORK_ERROR, Result.CODE_TIMEOUT, "temporarily_unavailable"]:
		var cached: RefCounted = _from_cache(ref, representation, false, pin_delivery_id)
		r = cached  # a cache miss is reported as temporarily_unavailable, never as another version
	return _finish(key, r)


func _finish(key: String, r: RefCounted) -> RefCounted:
	if r.ok:
		state_changed.emit(key, STATE_VERIFIED, 1.0)
	elif r.code == Result.CODE_CANCELLED:
		state_changed.emit(key, STATE_CANCELLED, 0.0)
	else:
		state_changed.emit(key, STATE_FAILED, 0.0)
	return r


# --- network path ------------------------------------------------------------------------------------------

func _from_network(ref: RefCounted, representation: String, token: RefCounted, pin: String) -> RefCounted:
	var res: RefCounted = await _client.resolve(ref.get("library_id"), [ref], {"representations": [representation]})
	if not res.ok:
		return res
	var entry: RefCounted = _pick_entry(res.value["entries"], ref)
	if not entry.ok:
		return entry
	var e: Dictionary = entry.value
	var delivery: Dictionary = _pick_delivery(e, representation, pin)
	if delivery.is_empty():
		if pin != "":
			return Result.fail("integrity_mismatch", "server does not offer the locked delivery %s" % pin)
		return Result.fail("unsupported_representation", "no delivery for %s" % representation)
	var desc: RefCounted = await _fetch_descriptor(ref, e)
	if not desc.ok:
		return desc
	var man: RefCounted = await _fetch_manifest(ref, representation, delivery, desc.value)
	if not man.ok:
		return man
	var files: RefCounted = await _fetch_files(ref, man.value, token)
	if not files.ok:
		return files
	_remember(ref, representation, desc.value, man.value)
	return Result.success({"descriptor": desc.value, "manifest": man.value, "files": files.value,
			"delivery_id": man.value.data["delivery_id"], "source": "network"})


func _pick_entry(entries: Array, ref: RefCounted) -> RefCounted:
	if entries.size() != 1 or not entries[0] is Dictionary:
		return Result.fail(Result.CODE_INVALID_RESPONSE, "resolve must return exactly one entry")
	var e: Dictionary = entries[0]
	var echoed: RefCounted = AssetRef.parse(e.get("asset_ref"))
	if not echoed.ok or not echoed.value.equals(ref) or e.get("asset_key") != ref.key():
		return Result.fail(Result.CODE_INVALID_RESPONSE, "resolve entry does not echo the requested reference")
	if e.get("state") == "ready":
		return Result.success(e)
	var err: Variant = e.get("error")
	var code: String = str((err as Dictionary).get("code", "")) if err is Dictionary else ""
	if not Result.is_known_server_code(code):
		return Result.fail(Result.CODE_INVALID_RESPONSE, "resolve entry in state %s" % str(e.get("state")))
	return Result.fail(code, str((err as Dictionary).get("message", "")).left(1024), code in Result.RETRYABLE_CODES)


## Deterministic choice if several profiles offer the representation: lowest delivery_id, or exactly `pin`.
func _pick_delivery(entry: Dictionary, representation: String, pin: String = "") -> Dictionary:
	var best: Dictionary = {}
	for d: Variant in entry.get("deliveries", []):
		if d is Dictionary and d.get("representation") == representation and (pin == "" or d.get("delivery_id") == pin):
			if best.is_empty() or str(d.get("delivery_id")) < str(best.get("delivery_id")):
				best = d
	return best


func _fetch_descriptor(ref: RefCounted, entry: Dictionary) -> RefCounted:
	var sha: String = str(entry.get("descriptor_sha256"))
	var raw: PackedByteArray = str(entry.get("descriptor_json", "")).to_utf8_buffer()
	if not Schema.matches("sha256", sha):
		return Result.fail(Result.CODE_INVALID_RESPONSE, "resolve entry lacks descriptor_sha256")
	if Canonical.sha256_hex(raw) != sha:  # the JSON string round-trip should be exact; fall back to the raw route
		var got: RefCounted = await _client.descriptor_bytes(ref.get("library_id"), ref.get("asset_id"), ref.get("version_id"))
		if not got.ok:
			return got
		raw = got.value["bytes"]
	if Canonical.sha256_hex(raw) != sha:
		return Result.fail("integrity_mismatch", "descriptor bytes do not match advertised sha256")
	var parsed: RefCounted = Descriptor.parse_bytes(raw)
	if not parsed.ok:
		return parsed
	if not parsed.value.asset_ref.equals(ref):
		return Result.fail("integrity_mismatch", "descriptor names a different asset reference")
	var stored: RefCounted = _cache.store_document("descriptors", sha, raw)
	return parsed if stored.ok else stored


func _fetch_manifest(ref: RefCounted, representation: String, delivery: Dictionary, desc: RefCounted) -> RefCounted:
	var sha: String = str(delivery.get("manifest_sha256"))
	var got: RefCounted = await _client.manifest_bytes(ref.get("library_id"), str(delivery.get("delivery_id")))
	if not got.ok:
		return got
	var raw: PackedByteArray = got.value["bytes"]
	if not Schema.matches("sha256", sha) or Canonical.sha256_hex(raw) != sha:
		return Result.fail("integrity_mismatch", "manifest bytes do not match advertised sha256")
	var parsed: RefCounted = Manifest.parse_bytes(raw)
	if not parsed.ok:
		return parsed
	var d: Dictionary = parsed.value.data
	if not parsed.value.asset_ref.equals(ref) or d["delivery_id"] != delivery.get("delivery_id") \
			or d["representation"] != representation or d["descriptor_sha256"] != desc.raw_sha256:
		return Result.fail("integrity_mismatch", "manifest does not match the requested exact reference")
	var stored: RefCounted = _cache.store_document("manifests", sha, raw)
	return parsed if stored.ok else stored


func _fetch_files(ref: RefCounted, manifest: RefCounted, token: RefCounted) -> RefCounted:
	var files: Array = manifest.data["files"]
	var total: float = 0.0
	for f: Dictionary in files:
		total += float(f["size"])
	var key: String = ref.key()
	var done: float = 0.0
	var paths: Dictionary = {}
	for f: Dictionary in files:
		if token != null and token.is_cancelled():
			return Result.fail(Result.CODE_CANCELLED, "preparation cancelled")
		state_changed.emit(key, STATE_DOWNLOADING, done / maxf(total, 1.0))
		var sha: String = f["sha256"]
		if not _cache.has_blob_sized(sha, int(f["size"])):
			var r: RefCounted = await _download_file(ref, f, token)
			if not r.ok:
				return r
		paths[f["path"]] = _cache.blob_path(sha)
		done += float(f["size"])
	return Result.success(paths)


func _download_file(ref: RefCounted, f: Dictionary, token: RefCounted) -> RefCounted:
	var staging: String = _cache.staging_path(f["sha256"])
	var r: RefCounted = await _client.download_artifact(ref.get("library_id"), f["artifact_id"], staging,
			f["sha256"], int(f["size"]), token)
	if not r.ok:
		return r
	return _cache.install_from_staging(staging, f["sha256"], int(f["size"]))


func _remember(ref: RefCounted, representation: String, desc: RefCounted, man: RefCounted) -> void:
	var key: String = ref.key()
	var index: Dictionary = _cache.read_ref_index(key)
	var entries: Dictionary = index.get("entries", {})
	entries[representation] = {"descriptor_sha256": desc.raw_sha256, "delivery_id": man.data["delivery_id"],
			"manifest_sha256": man.raw_sha256}
	_cache.write_ref_index(key, {"asset_key": key, "entries": entries})


# --- cache path --------------------------------------------------------------------------------------------

func _from_cache(ref: RefCounted, representation: String, explicit_offline: bool, pin: String = "") -> RefCounted:
	var miss: RefCounted = Result.fail("temporarily_unavailable",
			"offline and exact version is not fully cached" if explicit_offline else "server unreachable and exact version is not fully cached",
			true)
	var entry: Variant = _cache.read_ref_index(ref.key()).get("entries", {}).get(representation)
	if not entry is Dictionary or (pin != "" and entry.get("delivery_id") != pin):
		return miss
	var desc: RefCounted = Descriptor.parse_bytes(_cache.load_document("descriptors", str(entry.get("descriptor_sha256"))))
	var man: RefCounted = Manifest.parse_bytes(_cache.load_document("manifests", str(entry.get("manifest_sha256"))))
	if not desc.ok or not man.ok or not desc.value.asset_ref.equals(ref) or not man.value.asset_ref.equals(ref):
		return miss
	var paths: Dictionary = {}
	for f: Dictionary in man.value.data["files"]:
		if not _cache.has_blob_sized(f["sha256"], int(f["size"])):
			return miss
		paths[f["path"]] = _cache.blob_path(f["sha256"])
	return Result.success({"descriptor": desc.value, "manifest": man.value, "files": paths,
			"delivery_id": man.value.data["delivery_id"], "source": "cache"})
