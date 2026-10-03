@tool
extends RefCounted
# Local publish journal: .assetstudio/publish/journal.json (git-ignored, canonical JSON, no secrets). One entry per
# reviewed publication intent, keyed by a hash of everything that was reviewed. The idempotency key is generated
# once per intent and stored BEFORE the commit request is sent, so a retry after a lost response can first ask the
# server about that key (publication-operations/{key}) instead of publishing twice.

const Canonical = preload("res://addons/assetstudio/core/as_canonical.gd")
const CJson = preload("res://addons/assetstudio/core/as_canonical_json.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")

const REL: String = ".assetstudio/publish/journal.json"


static func intent_id(reviewed: Dictionary) -> String:
	var enc: RefCounted = CJson.encode(reviewed)
	return Canonical.sha256_hex(enc.value) if enc.ok else ""


static func new_key() -> String:
	return "asp_" + Crypto.new().generate_random_bytes(16).hex_encode()


## {intent_id: entry}. A missing or unreadable journal is empty: the server operation query is the authority.
static func entries(root: String) -> Dictionary:
	var parsed: RefCounted = CJson.parse_strict_utf8(Fs.read_bytes(root.path_join(REL)))
	if parsed.ok and parsed.value is Dictionary and (parsed.value as Dictionary).get("entries") is Dictionary:
		return (parsed.value["entries"] as Dictionary).duplicate(true)
	return {}


static func find(root: String, id: String) -> Dictionary:
	var all: Dictionary = entries(root)
	return (all[id] as Dictionary).duplicate(true) if all.has(id) else {}


## Returns OK or an Error.
static func save(root: String, id: String, entry: Dictionary) -> int:
	var all: Dictionary = entries(root)
	all[id] = entry
	var enc: RefCounted = CJson.encode({"schema_version": 1, "entries": all})
	if not enc.ok:
		return ERR_INVALID_DATA
	return Fs.write_atomic(root.path_join(REL), enc.value)
