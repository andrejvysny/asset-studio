extends Node
# Long-poll loop over ASLibraryClient.changes(). Emits invalidation hints only; it never refreshes, downloads
# or prunes anything itself. The cursor is opaque and kept in memory (persist `cursor` if you need restart).

const CancelToken = preload("res://addons/assetstudio/core/as_cancel_token.gd")

## events: Array of {type, library_id[, asset_id]} dictionaries from the server.
signal changed(events: Array)
## The server asked for a full re-read (restart, expired cursor); `cursor` already points at "now".
signal reset_required
signal failed(code: String)

var cursor: String = ""
var poll_timeout_s: float = 20.0
var min_interval_s: float = 0.05
var max_backoff_s: float = 30.0

var _client: Node = null
var _token: RefCounted = null
var _running: bool = false


func start(client: Node, initial_cursor: String = "") -> void:
	if _running:
		return
	_client = client
	cursor = initial_cursor
	_running = true
	_token = CancelToken.new()
	_loop(_token)


func stop() -> void:
	_running = false
	if _token != null:
		_token.cancel()


func is_running() -> bool:
	return _running


func _loop(token: RefCounted) -> void:
	var failures: int = 0
	while _running and not token.is_cancelled():
		var r: RefCounted = await _client.changes(cursor, poll_timeout_s, token)
		if not _running or token.is_cancelled():
			break
		if r.ok:
			failures = 0
			_handle(r.value)
			await get_tree().create_timer(min_interval_s).timeout
		else:
			failures += 1
			failed.emit(r.code)
			await get_tree().create_timer(minf(max_backoff_s, pow(2.0, failures - 1))).timeout


func _handle(v: Dictionary) -> void:
	cursor = str(v.get("cursor", cursor))
	if v.get("reset_required", false):
		reset_required.emit()
		return
	var events: Variant = v.get("events", [])
	if events is Array and not (events as Array).is_empty():
		changed.emit(events)
