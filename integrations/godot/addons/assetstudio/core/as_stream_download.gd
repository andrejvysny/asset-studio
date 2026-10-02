@tool
extends RefCounted
# Streams one GET response body into a file with HTTPClient. HTTPRequest is not used for artifact bodies
# because it deletes its download file when a transfer fails, which would make Range resume impossible, and
# because it exposes neither status nor headers after a mid-body failure.
# Redirects are never followed. Credentials only go to the configured host.

const ERR_NONE: String = ""
const ERR_CANCELLED: String = "cancelled"
const ERR_TIMEOUT: String = "timeout"
const ERR_NETWORK: String = "network"
const ERR_LIMIT: String = "limit"
const READ_CHUNK: int = 1048576


## Returns {"error": ERR_*, "status": int, "headers": Dictionary (lowercase keys), "bytes": int}.
## The body is written to `part_path` (truncated first); bytes received before a failure are kept there.
## `host`/`port`/`tls` come from the validated registry URL; `max_body` caps the bytes accepted.
static func fetch(owner: Node, host: String, port: int, tls: bool, path: String, headers: PackedStringArray,
		part_path: String, idle_timeout_s: float, max_body: int, token: RefCounted) -> Dictionary:
	var out: Dictionary = {"error": ERR_NETWORK, "status": 0, "headers": {}, "bytes": 0}
	var client := HTTPClient.new()
	client.read_chunk_size = READ_CHUNK
	if client.connect_to_host(host, port, TLSOptions.client() if tls else null) != OK:
		return out
	var failed: String = await _wait(owner, client, [HTTPClient.STATUS_CONNECTING, HTTPClient.STATUS_RESOLVING],
			HTTPClient.STATUS_CONNECTED, idle_timeout_s, token)
	if failed == ERR_NONE and client.request(HTTPClient.METHOD_GET, path, headers) != OK:
		failed = ERR_NETWORK
	if failed == ERR_NONE:
		failed = await _wait(owner, client, [HTTPClient.STATUS_REQUESTING], HTTPClient.STATUS_BODY, idle_timeout_s, token)
	if failed != ERR_NONE or not client.has_response():
		client.close()
		out["error"] = failed if failed != ERR_NONE else ERR_NETWORK
		return out
	out["status"] = client.get_response_code()
	var raw_headers: Dictionary = client.get_response_headers_as_dictionary()
	var lowered: Dictionary = {}
	for k: Variant in raw_headers:
		lowered[str(k).to_lower()] = str(raw_headers[k])
	out["headers"] = lowered
	out["error"] = await _read_body(owner, client, part_path, idle_timeout_s, max_body, token, out)
	client.close()
	return out


static func _wait(owner: Node, client: HTTPClient, busy: Array, done: int, idle_s: float, token: RefCounted) -> String:
	var started: int = Time.get_ticks_msec()
	while true:
		client.poll()
		var status: int = client.get_status()
		if status == done or (done == HTTPClient.STATUS_BODY and status == HTTPClient.STATUS_CONNECTED):
			return ERR_NONE
		if not busy.has(status):
			return ERR_NETWORK
		if token != null and token.is_cancelled():
			return ERR_CANCELLED
		if Time.get_ticks_msec() - started > idle_s * 1000.0:
			return ERR_TIMEOUT
		await owner.get_tree().process_frame
	return ERR_NETWORK


static func _read_body(owner: Node, client: HTTPClient, part_path: String, idle_s: float, max_body: int,
		token: RefCounted, out: Dictionary) -> String:
	var length: int = client.get_response_body_length()
	if length > max_body:
		return ERR_LIMIT
	var f: FileAccess = FileAccess.open(part_path, FileAccess.WRITE)
	if f == null:
		return ERR_NETWORK
	var last_progress: int = Time.get_ticks_msec()
	var err: String = ERR_NONE
	while client.get_status() == HTTPClient.STATUS_BODY:
		if token != null and token.is_cancelled():
			err = ERR_CANCELLED
			break
		client.poll()
		var chunk: PackedByteArray = client.read_response_body_chunk()
		if chunk.is_empty():
			if Time.get_ticks_msec() - last_progress > idle_s * 1000.0:
				err = ERR_TIMEOUT
				break
			await owner.get_tree().process_frame
			continue
		last_progress = Time.get_ticks_msec()
		out["bytes"] = int(out["bytes"]) + chunk.size()
		if int(out["bytes"]) > max_body:
			err = ERR_LIMIT
			break
		f.store_buffer(chunk)
	f.close()
	if err == ERR_NONE and length >= 0 and int(out["bytes"]) != length:
		err = ERR_NETWORK  # connection ended before Content-Length bytes arrived
	elif err == ERR_NONE and length < 0 and client.get_status() == HTTPClient.STATUS_CONNECTION_ERROR:
		err = ERR_NETWORK
	return err
