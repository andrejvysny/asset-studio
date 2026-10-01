extends RefCounted
# Typed result value. Errors are expected outcomes, never engine errors.
# Codes: contracts/godot-integration/v1/error-codes.json plus client-local codes below.

const CODE_NETWORK_ERROR: String = "network_error"
const CODE_TIMEOUT: String = "timeout"
const CODE_CANCELLED: String = "cancelled"
const CODE_INVALID_RESPONSE: String = "invalid_response"

const SERVER_CODES: PackedStringArray = [
	"unauthorized", "forbidden", "server_identity_mismatch", "unsupported_contract", "asset_not_found",
	"version_unavailable", "delivery_preparing", "unsupported_representation", "integrity_mismatch",
	"unsafe_package", "unsupported_source_dependency", "resource_limit", "stale_pointer",
	"idempotency_conflict", "cancelled", "temporarily_unavailable", "invalid_request", "preview_expired",
]
const RETRYABLE_CODES: PackedStringArray = [
	"delivery_preparing", "temporarily_unavailable", "network_error", "timeout",
]

var ok: bool = true
var value: Variant = null
var code: String = ""
var message: String = ""
var retryable: bool = false
var details: Dictionary = {}


static func success(v: Variant = null) -> RefCounted:
	var r: RefCounted = new()
	r.set("value", v)
	return r


static func fail(error_code: String, msg: String = "", is_retryable: bool = false, extra: Dictionary = {}) -> RefCounted:
	var r: RefCounted = new()
	r.set("ok", false)
	r.set("code", error_code)
	r.set("message", msg)
	r.set("retryable", is_retryable)
	r.set("details", extra)
	return r


static func is_known_server_code(error_code: String) -> bool:
	return SERVER_CODES.has(error_code)


## Lets callers log without ever touching credentials: results never carry headers.
func describe() -> String:
	return "ok" if ok else "%s: %s" % [code, message]
