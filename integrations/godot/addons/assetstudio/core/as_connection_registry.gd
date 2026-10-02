@tool
extends RefCounted
# Maps stable server IDs to device-local endpoints. Credentials live in a separate file under user://
# (never res://), are never printed and never placed in URLs.
#
# Permission limitation: files are chmod 0600 via FileAccess.set_unix_permissions, which is a no-op error on
# platforms without POSIX modes (Windows, and effectively iOS where the app sandbox is the protection).
# Prefer OS credential storage where the host app has it; this file is the documented dev fallback.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")

const DEFAULT_DIR: String = "user://assetstudio"
const CONNECTIONS_FILE: String = "connections.json"
const CREDENTIALS_FILE: String = "credentials.json"
const _URL_PATTERN: String = "^((?i)https?)://(\\[[0-9A-Fa-f:.]+\\]|[A-Za-z0-9.-]+)(:[0-9]{1,5})?/?$"

static var _url_re: RegEx = null

var dir: String = DEFAULT_DIR
var _connections: Dictionary = {}  # server_id -> {"base_url": String, "allow_insecure_lan": bool}
var _credentials: Dictionary = {}  # server_id -> token


func _init(storage_dir: String = DEFAULT_DIR) -> void:
	dir = storage_dir
	_load_files()


## Validates and normalizes a base URL. Returns ASResult with value {"base_url", "scheme", "host", "port", "loopback"}.
static func parse_base_url(url: String, allow_insecure_lan: bool) -> RefCounted:
	if _url_re == null:
		_url_re = RegEx.create_from_string(_URL_PATTERN)
	var m: RegExMatch = _url_re.search(url)
	if m == null or m.get_string() != url:
		return Result.fail("invalid_request", "base URL must be http(s)://host[:port] without path, query or userinfo")
	var scheme: String = m.get_string(1).to_lower()
	var host: String = m.get_string(2).to_lower()
	var port_text: String = m.get_string(3)
	if not port_text.is_empty():
		var port: int = int(port_text.substr(1))
		if port < 1 or port > 65535:
			return Result.fail("invalid_request", "port out of range")
	var loopback: bool = is_loopback_host(host)
	if scheme == "http" and not loopback and not allow_insecure_lan:
		return Result.fail("invalid_request", "cleartext http to a non-loopback host requires allow_insecure_lan")
	var base: String = "%s://%s%s" % [scheme, host, port_text]
	var port: int = int(port_text.substr(1)) if not port_text.is_empty() else (443 if scheme == "https" else 80)
	return Result.success({"base_url": base, "scheme": scheme, "host": host.trim_prefix("[").trim_suffix("]"),
			"port": port, "tls": scheme == "https", "loopback": loopback})


static func is_loopback_host(host: String) -> bool:
	return host == "localhost" or host == "[::1]" or host.begins_with("127.")


func set_connection(server_id: String, base_url: String, allow_insecure_lan: bool = false) -> RefCounted:
	if not Schema.matches("server_id", server_id):
		return Result.fail("invalid_request", "invalid server_id")
	var parsed: RefCounted = parse_base_url(base_url, allow_insecure_lan)
	if not parsed.ok:
		return parsed
	var info: Dictionary = parsed.value
	if info["scheme"] == "http" and not info["loopback"]:
		push_warning("AssetStudio: cleartext http to %s; bearer tokens can be intercepted on the network" % info["host"])
	_connections[server_id] = {"base_url": info["base_url"], "allow_insecure_lan": allow_insecure_lan}
	return _save(CONNECTIONS_FILE, {"connections": _connections})


## Re-validates on every read: the config file may have been edited by hand.
func get_connection(server_id: String) -> RefCounted:
	if not _connections.has(server_id):
		return Result.fail("invalid_request", "unknown server_id")
	var c: Dictionary = _connections[server_id]
	var parsed: RefCounted = parse_base_url(c["base_url"], c["allow_insecure_lan"])
	if not parsed.ok:
		return parsed
	var info: Dictionary = (parsed.value as Dictionary).duplicate()
	info["allow_insecure_lan"] = c["allow_insecure_lan"]
	return Result.success(info)


func remove_connection(server_id: String) -> RefCounted:
	_connections.erase(server_id)
	_credentials.erase(server_id)
	var r: RefCounted = _save(CONNECTIONS_FILE, {"connections": _connections})
	if not r.ok:
		return r
	return _save(CREDENTIALS_FILE, {"credentials": _credentials})


func server_ids() -> PackedStringArray:
	var ids := PackedStringArray()
	for k: String in _connections.keys():
		ids.append(k)
	ids.sort()
	return ids


func set_credential(server_id: String, token: String) -> RefCounted:
	if not Schema.matches("server_id", server_id) or token.is_empty() or token.contains("\n"):
		return Result.fail("invalid_request", "invalid server_id or token")
	_credentials[server_id] = token
	return _save(CREDENTIALS_FILE, {"credentials": _credentials})


func has_credential(server_id: String) -> bool:
	return _credentials.has(server_id)


## Only the client's header builder should call this.
func credential_for_request(server_id: String) -> String:
	return _credentials.get(server_id, "")


func _load_files() -> void:
	var conns: Variant = _read(CONNECTIONS_FILE).get("connections", {})
	if conns is Dictionary:
		_connections = conns
	var creds: Variant = _read(CREDENTIALS_FILE).get("credentials", {})
	if creds is Dictionary:
		_credentials = creds


func _read(file: String) -> Dictionary:
	var path: String = dir.path_join(file)
	if not FileAccess.file_exists(path):
		return {}
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(path))
	return parsed if parsed is Dictionary else {}


## Atomic write (tmp + rename); owner-only permissions are applied before the content becomes visible.
func _save(file: String, content: Dictionary) -> RefCounted:
	DirAccess.make_dir_recursive_absolute(dir)
	var path: String = dir.path_join(file)
	var tmp: String = path + ".tmp"
	var f: FileAccess = FileAccess.open(tmp, FileAccess.WRITE)
	if f == null:
		return Result.fail("temporarily_unavailable", "cannot write %s" % file)
	f.close()
	FileAccess.set_unix_permissions(tmp, FileAccess.UNIX_READ_OWNER | FileAccess.UNIX_WRITE_OWNER)
	f = FileAccess.open(tmp, FileAccess.WRITE)
	if f == null:
		return Result.fail("temporarily_unavailable", "cannot write %s" % file)
	f.store_string(JSON.stringify(content))
	f.close()
	if DirAccess.rename_absolute(tmp, path) != OK:
		return Result.fail("temporarily_unavailable", "cannot replace %s" % file)
	return Result.success()
