extends Node
# Authenticated async client for the AssetStudio integration API v1. Every method returns an ASResult
# (await it). Notes:
#  - Bearer header only; the token is never logged, put in a URL, or kept in a result.
#  - Paths are built from validated IDs only; server-supplied URLs are never followed (redirects disabled).
#  - capabilities.server_id must equal the registry's server_id before any other authenticated request.
#  - Retries (max `max_retries`, exponential backoff) only for idempotent requests failing retryably.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")
const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")
const Version = preload("res://addons/assetstudio/core/as_version.gd")
const Util = preload("res://addons/assetstudio/core/as_http_util.gd")
const Stream = preload("res://addons/assetstudio/core/as_stream_download.gd")

signal slot_freed

const API_PREFIX: String = "/api/integration/v1"
const MAX_METADATA_BYTES: int = 16777216
const _CONTENT_RANGE: String = "^bytes ([0-9]+)-([0-9]+)/([0-9]+)$"

var timeout_s: float = 20.0
var download_timeout_s: float = 300.0
var max_retries: int = 2
var backoff_base_s: float = 0.5
var server_id: String = ""
var server_info: Dictionary = {}

var _registry: RefCounted = null
var _identity_ok: bool = false
var _limits: Dictionary = {"meta": 4, "download": 2}
var _active: Dictionary = {"meta": 0, "download": 0}
var peak_downloads: int = 0  # observed maximum of concurrent downloads (test hook)
var _range_re: RegEx = RegEx.create_from_string(_CONTENT_RANGE)


## One response of the transport layer; finished fires once, from completion or cancellation.
class Pending extends RefCounted:
	signal finished
	var done: bool = false
	var cancelled: bool = false
	var result: int = -1
	var code: int = 0
	var headers: PackedStringArray = PackedStringArray()
	var body: PackedByteArray = PackedByteArray()

	func on_completed(r: int, c: int, h: PackedStringArray, b: PackedByteArray) -> void:
		result = r
		code = c
		headers = h
		body = b
		_finish()

	func on_cancel() -> void:
		cancelled = true
		_finish()

	func _finish() -> void:
		if done:
			return
		done = true
		finished.emit()


func setup(registry: RefCounted, expected_server_id: String) -> void:
	_registry = registry
	server_id = expected_server_id
	_identity_ok = false


func set_concurrency(metadata: int, downloads: int) -> void:
	_limits = {"meta": maxi(1, metadata), "download": maxi(1, downloads)}


# --- public API --------------------------------------------------------------------------------------------

func health() -> RefCounted:
	return await _json_request(HTTPClient.METHOD_GET, "/health", {}, null, false, false)


## First authenticated call; verifies server identity and contract/API versions.
func capabilities() -> RefCounted:
	var r: RefCounted = await _json_request(HTTPClient.METHOD_GET, "/capabilities", {}, null, true, false)
	if not r.ok:
		return r
	var caps: Dictionary = r.value
	if caps.get("server_id", "") != server_id:
		_identity_ok = false
		return Result.fail("server_identity_mismatch", "endpoint identifies as a different server")
	if int(caps.get("api_version", 0)) != Version.API_VERSION or int(caps.get("contract_version", 0)) != Version.CONTRACT_VERSION:
		return Result.fail("unsupported_contract", "server contract/API version is not supported")
	_identity_ok = true
	server_info = caps
	return r


func libraries() -> RefCounted:
	return await _json_request(HTTPClient.METHOD_GET, "/libraries", {}, null, true, true)


func list_assets(library: String, query: Dictionary = {}, cursor: String = "", limit: int = 60) -> RefCounted:
	var bad: RefCounted = _check_ids({"library": library})
	if bad != null:
		return bad
	if limit < 1 or limit > 200:
		return Result.fail("invalid_request", "limit must be 1..200")
	var q: Dictionary = {"limit": str(limit)}
	for key: String in ["q", "category", "tags", "kind"]:
		if query.has(key):
			q[key] = ",".join(PackedStringArray(query[key])) if query[key] is Array else str(query[key])
	if cursor != "":
		q["cursor"] = cursor
	return await _json_request(HTTPClient.METHOD_GET, "/libraries/%s/assets" % library, q, null, true, true)


func asset(library: String, asset_id: String) -> RefCounted:
	var bad: RefCounted = _check_ids({"library": library, "asset_id": asset_id})
	if bad != null:
		return bad
	return await _json_request(HTTPClient.METHOD_GET, "/libraries/%s/assets/%s" % [library, asset_id], {}, null, true, true)


func version(library: String, asset_id: String, version_id: String) -> RefCounted:
	var bad: RefCounted = _check_ids({"library": library, "asset_id": asset_id, "version_id": version_id})
	if bad != null:
		return bad
	var path: String = "/libraries/%s/assets/%s/versions/%s" % [library, asset_id, version_id]
	return await _json_request(HTTPClient.METHOD_GET, path, {}, null, true, true)


## refs: Array of ASAssetRef or Dictionary. Returns the parsed response ({"entries": [...]}).
func resolve(library: String, refs: Array, target: Dictionary = {}) -> RefCounted:
	var bad: RefCounted = _check_ids({"library": library})
	if bad != null:
		return bad
	var body_refs: Array = []
	for ref: Variant in refs:
		body_refs.append(ref.call("to_dict") if ref is Object else ref)
	var body: Dictionary = {"refs": body_refs}
	if not target.is_empty():
		body["target"] = target
	var r: RefCounted = await _json_request(HTTPClient.METHOD_POST, "/libraries/%s/resolve" % library, {}, body, true, true)
	if r.ok and not (r.value.get("entries") is Array):
		return Result.fail(Result.CODE_INVALID_RESPONSE, "resolve response lacks entries")
	return r


## value = {"bytes": PackedByteArray, "sha256": computed, "claimed_sha256": X-Content-SHA256 or ""}.
func descriptor_bytes(library: String, asset_id: String, version_id: String) -> RefCounted:
	var bad: RefCounted = _check_ids({"library": library, "asset_id": asset_id, "version_id": version_id})
	if bad != null:
		return bad
	var path: String = "/libraries/%s/assets/%s/versions/%s/descriptor" % [library, asset_id, version_id]
	return await _raw_document(path)


func manifest_bytes(library: String, delivery_id: String) -> RefCounted:
	var bad: RefCounted = _check_ids({"library": library, "delivery_id": delivery_id})
	if bad != null:
		return bad
	return await _raw_document("/libraries/%s/deliveries/%s/manifest" % [library, delivery_id])


func changes(cursor: String = "", poll_timeout_s: float = 20.0, cancel_token: RefCounted = null) -> RefCounted:
	if not Schema.matches("url_id", cursor) and cursor != "":
		return Result.fail("invalid_request", "malformed cursor")
	var q: Dictionary = {"timeout_s": str(clampf(poll_timeout_s, 0.0, 20.0))}
	if cursor != "":
		q["cursor"] = cursor
	var opts: Dictionary = {"timeout": poll_timeout_s + timeout_s, "kind": "poll"}
	return await _json_request(HTTPClient.METHOD_GET, "/changes", q, null, true, true, opts, cancel_token)


## Streams the artifact into dest_path (a staging file; resumed with Range when partial bytes exist), then
## checks size and sha256. On mismatch dest_path is deleted and integrity_mismatch is returned. Does not install.
func download_artifact(library: String, artifact_id: String, dest_path: String, expected_sha256: String,
		expected_size: int, cancel_token: RefCounted = null) -> RefCounted:
	var bad: RefCounted = _check_ids({"library": library, "artifact_id": artifact_id})
	if bad != null:
		return bad
	if not Schema.matches("sha256", expected_sha256) or expected_size < 0:
		return Result.fail("invalid_request", "expected sha256/size required")
	var ident: RefCounted = await _ensure_identity()
	if not ident.ok:
		return ident
	await _acquire("download")
	var r: RefCounted = await _download_with_retries(library, artifact_id, dest_path, expected_sha256, expected_size, cancel_token)
	_release("download")
	return r


# --- downloads ---------------------------------------------------------------------------------------------

func _download_with_retries(library: String, artifact_id: String, dest: String, sha: String, size: int,
		token: RefCounted) -> RefCounted:
	DirAccess.make_dir_recursive_absolute(dest.get_base_dir())
	var attempt: int = 0
	while true:
		if token != null and token.is_cancelled():
			return Result.fail(Result.CODE_CANCELLED, "download cancelled")
		var r: RefCounted = await _download_once(library, artifact_id, dest, size, token)
		if r.ok:
			return _verify_download(dest, sha, size)
		if not r.retryable or attempt >= max_retries:
			return r
		await _backoff(attempt, token)
		attempt += 1
	return Result.fail(Result.CODE_NETWORK_ERROR, "unreachable")


func _verify_download(dest: String, sha: String, size: int) -> RefCounted:
	var f: FileAccess = FileAccess.open(dest, FileAccess.READ)
	var actual_size: int = f.get_length() if f != null else -1
	f = null
	if actual_size != size or FileAccess.get_sha256(dest) != sha:
		DirAccess.remove_absolute(dest)
		return Result.fail("integrity_mismatch", "downloaded bytes do not match expected size/sha256")
	return Result.success({"path": dest, "size": size, "sha256": sha})


func _download_once(library: String, artifact_id: String, dest: String, size: int, token: RefCounted) -> RefCounted:
	var ep: RefCounted = _endpoint()
	if not ep.ok:
		return ep
	var offset: int = _file_size(dest)
	if offset > size or offset < 0:
		DirAccess.remove_absolute(dest)
		offset = 0
	if offset == size:
		if offset == 0:
			FileAccess.open(dest, FileAccess.WRITE).close()
		return Result.success()
	var part: String = dest + ".part"
	DirAccess.remove_absolute(part)
	var headers: PackedStringArray = _headers(ep.value["token"], false)
	if offset > 0:
		headers.append("Range: bytes=%d-" % offset)
	var path: String = API_PREFIX + "/libraries/%s/artifacts/%s/content" % [library, artifact_id]
	var info: Dictionary = ep.value
	var res: Dictionary = await Stream.fetch(self, info["host"], info["port"], info["tls"], path, headers, part,
			download_timeout_s, size, token)
	return _finish_download(res, part, dest, offset, size)


func _finish_download(res: Dictionary, part: String, dest: String, offset: int, size: int) -> RefCounted:
	var status: int = res["status"]
	var hdrs: Dictionary = res["headers"]
	var err: String = res["error"]
	if err == Stream.ERR_NONE and status >= 400:
		var body: PackedByteArray = FileAccess.get_file_as_bytes(part)
		DirAccess.remove_absolute(part)
		if status == 416 and offset > 0:
			DirAccess.remove_absolute(dest)
			return Result.fail(Result.CODE_NETWORK_ERROR, "range not satisfiable; restarting", true)
		return Util.http_error(status, body.slice(0, 65536))
	if err != Stream.ERR_LIMIT:
		_keep_received(part, dest, offset, size, status, hdrs)
	else:
		DirAccess.remove_absolute(part)
	match err:
		Stream.ERR_NONE:
			if status != 200 and status != 206:
				return Result.fail(Result.CODE_INVALID_RESPONSE, "unexpected HTTP %d" % status)
			return Result.success() if FileAccess.file_exists(dest) else Result.fail(Result.CODE_INVALID_RESPONSE, "no body")
		Stream.ERR_CANCELLED:
			return Result.fail(Result.CODE_CANCELLED, "download cancelled")
		Stream.ERR_TIMEOUT:
			return Result.fail(Result.CODE_TIMEOUT, "download timed out", true)
		Stream.ERR_LIMIT:
			return Result.fail("resource_limit", "response larger than the manifest declares")
	return Result.fail(Result.CODE_NETWORK_ERROR, "network error", true)


## Applies received bytes to the staging file: a valid 206 extends it; a 200 (Range ignored or not sent)
## replaces it, never appends; anything else is discarded. Works for partial bodies after a failure too.
func _keep_received(part: String, dest: String, offset: int, size: int, status: int, hdrs: Dictionary) -> void:
	if not FileAccess.file_exists(part):
		return
	if status == 206 and _range_ok(hdrs, offset, size):
		_append_file(part, dest)
	elif status == 200:
		DirAccess.remove_absolute(dest)
		DirAccess.rename_absolute(part, dest)
	else:
		DirAccess.remove_absolute(part)


func _range_ok(hdrs: Dictionary, offset: int, size: int) -> bool:
	var m: RegExMatch = _range_re.search(str(hdrs.get("content-range", "")))
	return m != null and int(m.get_string(1)) == offset and int(m.get_string(3)) == size and int(m.get_string(2)) == size - 1


func _append_file(part: String, dest: String) -> RefCounted:
	var src: FileAccess = FileAccess.open(part, FileAccess.READ)
	var dst: FileAccess = FileAccess.open(dest, FileAccess.READ_WRITE)
	if src == null or dst == null:
		return Result.fail("temporarily_unavailable", "cannot append to staging file", true)
	dst.seek_end()
	while not src.eof_reached():
		var chunk: PackedByteArray = src.get_buffer(1048576)
		if chunk.is_empty():
			break
		dst.store_buffer(chunk)
	src = null
	dst.close()
	DirAccess.remove_absolute(part)
	return Result.success()


func _file_size(path: String) -> int:
	if not FileAccess.file_exists(path):
		return 0
	var f: FileAccess = FileAccess.open(path, FileAccess.READ)
	return f.get_length() if f != null else -1


# --- JSON/document requests --------------------------------------------------------------------------------

func _raw_document(path: String) -> RefCounted:
	var r: RefCounted = await _request(HTTPClient.METHOD_GET, path, {}, null, true, true)
	if not r.ok:
		return r
	var body: PackedByteArray = r.value["body"]
	var claimed: String = str(r.value["headers"].get("x-content-sha256", ""))
	return Result.success({"bytes": body, "sha256": Canonical.sha256_hex(body), "claimed_sha256": claimed})


func _json_request(method: int, path: String, query: Dictionary, body: Variant, authed: bool, idempotent: bool,
		opts: Dictionary = {}, token: RefCounted = null) -> RefCounted:
	var r: RefCounted = await _request(method, path, query, body, authed and path != "/capabilities", idempotent, opts, token, authed)
	if not r.ok:
		return r
	var json := JSON.new()
	var text: String = (r.value["body"] as PackedByteArray).get_string_from_utf8()
	if json.parse(text) != OK or not json.data is Dictionary:
		return Result.fail(Result.CODE_INVALID_RESPONSE, "response is not a JSON object")
	return Result.success(json.data)


## Metadata request: identity check (unless this is the capabilities call), slot, retries.
func _request(method: int, path: String, query: Dictionary, body: Variant, check_identity: bool, idempotent: bool,
		opts: Dictionary = {}, token: RefCounted = null, send_auth: bool = true) -> RefCounted:
	if check_identity:
		var ident: RefCounted = await _ensure_identity()
		if not ident.ok:
			return ident
	var bounded: bool = opts.get("kind", "meta") == "meta"
	if bounded:
		await _acquire("meta")
	var attempt: int = 0
	var r: RefCounted = null
	while true:
		r = await _attempt(method, path, query, body, send_auth, opts, token)
		if r.ok or not r.retryable or not idempotent or attempt >= max_retries:
			break
		await _backoff(attempt, token)
		attempt += 1
	if bounded:
		_release("meta")
	return r


func _attempt(method: int, path: String, query: Dictionary, body: Variant, send_auth: bool, opts: Dictionary,
		token: RefCounted) -> RefCounted:
	if token != null and token.is_cancelled():
		return Result.fail(Result.CODE_CANCELLED, "request cancelled")
	var ep: RefCounted = _endpoint(send_auth)
	if not ep.ok:
		return ep
	var url: String = ep.value["base_url"] + API_PREFIX + path + Util.query_string(query)
	var headers: PackedStringArray = _headers(ep.value["token"], body != null)
	var raw: PackedByteArray = JSON.stringify(body).to_utf8_buffer() if body != null else PackedByteArray()
	var topts: Dictionary = {"timeout": opts.get("timeout", timeout_s), "body_limit": MAX_METADATA_BYTES}
	var p: Pending = await _transport(method, url, headers, raw, topts, token)
	if p.cancelled or p.result != HTTPRequest.RESULT_SUCCESS:
		return _transport_error(p)
	if p.code < 200 or p.code > 299:
		return Util.http_error(p.code, p.body)
	return Result.success({"status": p.code, "headers": Util.header_dict(p.headers), "body": p.body})


# --- transport ---------------------------------------------------------------------------------------------

func _transport(method: int, url: String, headers: PackedStringArray, body: PackedByteArray, opts: Dictionary,
		token: RefCounted) -> Pending:
	var p := Pending.new()
	if token != null and token.is_cancelled():
		p.cancelled = true
		return p
	var http := HTTPRequest.new()
	http.timeout = opts["timeout"]
	http.max_redirects = 0
	http.accept_gzip = false
	http.body_size_limit = opts.get("body_limit", -1)
	add_child(http)
	http.request_completed.connect(p.on_completed)
	var on_cancel: Callable = func() -> void:
		http.cancel_request()
		p.on_cancel()
	if token != null:
		token.connect("cancelled", on_cancel)
	if http.request_raw(url, headers, method, body) != OK:
		p.on_completed(HTTPRequest.RESULT_CANT_CONNECT, 0, PackedStringArray(), PackedByteArray())
	if not p.done:
		await p.finished
	if token != null and token.is_connected("cancelled", on_cancel):
		token.disconnect("cancelled", on_cancel)
	http.queue_free()
	return p


func _transport_error(p: Pending) -> RefCounted:
	if p.cancelled:
		return Result.fail(Result.CODE_CANCELLED, "request cancelled")
	match p.result:
		HTTPRequest.RESULT_TIMEOUT:
			return Result.fail(Result.CODE_TIMEOUT, "request timed out", true)
		HTTPRequest.RESULT_BODY_SIZE_LIMIT_EXCEEDED:
			return Result.fail("resource_limit", "response larger than expected")
		HTTPRequest.RESULT_REDIRECT_LIMIT_REACHED:
			return Result.fail(Result.CODE_INVALID_RESPONSE, "unexpected redirect")
	return Result.fail(Result.CODE_NETWORK_ERROR, "network error", true, {"http_result": p.result})


# --- helpers -----------------------------------------------------------------------------------------------

func _ensure_identity() -> RefCounted:
	if _identity_ok:
		return Result.success()
	var r: RefCounted = await capabilities()
	return Result.success() if r.ok else r


func _endpoint(send_auth: bool = true) -> RefCounted:
	if _registry == null:
		return Result.fail("invalid_request", "client not configured")
	var conn: RefCounted = _registry.get_connection(server_id)
	if not conn.ok:
		return conn
	var secret: String = ""
	if send_auth:
		secret = _registry.credential_for_request(server_id)
		if secret.is_empty():
			return Result.fail("unauthorized", "no credential stored for this server")
	return Result.success({"base_url": conn.value["base_url"], "host": conn.value["host"], "port": conn.value["port"],
			"tls": conn.value["tls"], "token": secret})


func _headers(secret: String, has_body: bool) -> PackedStringArray:
	var h := PackedStringArray(["Accept: application/json", "User-Agent: AssetStudioAddon/%s" % Version.VERSION])
	if secret != "":
		h.append("Authorization: Bearer " + secret)
	if has_body:
		h.append("Content-Type: application/json")
	return h


## Returns null when every ID is a safe single path segment, else a failed result.
func _check_ids(ids: Dictionary) -> RefCounted:
	for k: String in ids:
		var v: String = ids[k]
		if not Schema.matches("url_id", v) or v == "." or v == "..":
			return Result.fail("invalid_request", "invalid %s" % k)
	return null


func _acquire(kind: String) -> void:
	while int(_active[kind]) >= int(_limits[kind]):
		await slot_freed
	_active[kind] = int(_active[kind]) + 1
	if kind == "download":
		peak_downloads = maxi(peak_downloads, int(_active[kind]))


func _release(kind: String) -> void:
	_active[kind] = int(_active[kind]) - 1
	slot_freed.emit()


func _backoff(attempt: int, token: RefCounted) -> void:
	if token != null and token.is_cancelled():
		return
	await get_tree().create_timer(backoff_base_s * pow(2.0, attempt)).timeout
