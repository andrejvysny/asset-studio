@tool
extends RefCounted
# Cooperative cancellation. The client connects to `cancelled` to abort in-flight HTTPRequests.

signal cancelled

var _cancelled: bool = false


func cancel() -> void:
	if _cancelled:
		return
	_cancelled = true
	cancelled.emit()


func is_cancelled() -> bool:
	return _cancelled
