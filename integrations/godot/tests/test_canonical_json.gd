extends "res://tests/project_test_base.gd"
# Canonical JSON writer against the Python-generated vectors (fixtures/vectors/canonical-json-v1.json).

const MAX_SKIPPED: int = 1  # the U+0000 case: Godot strings cannot hold NUL


func _vectors() -> Dictionary:
	var parsed: Variant = JSON.parse_string(fixture("vectors/canonical-json-v1.json").get_string_from_utf8())
	if not parsed is Dictionary:
		fail("cannot load canonical-json-v1.json")
		return {}
	return parsed


## Tagged input -> {"ok": bool, "v": Variant}; ok=false when Godot cannot represent it (NUL).
func _decode(t: Dictionary) -> Dictionary:
	if t.has("n"):
		return {"ok": true, "v": null}
	if t.has("b"):
		return {"ok": true, "v": t["b"]}
	if t.has("i"):
		return {"ok": true, "v": int(t["i"])}
	if t.has("f"):
		var text: String = t["f"]
		var f: float = NAN if text == "nan" else (INF if text == "inf" else (-INF if text == "-inf" else text.to_float()))
		return {"ok": true, "v": f}
	if t.has("s"):
		return _decode_string(t["s"])
	if t.has("a"):
		var arr: Array = []
		for item: Dictionary in t["a"]:
			var d: Dictionary = _decode(item)
			if not d["ok"]:
				return d
			arr.append(d["v"])
		return {"ok": true, "v": arr}
	var obj: Dictionary = {}
	for pair: Dictionary in t["o"]:
		var k: Dictionary = _decode_string(pair["k"])
		var v: Dictionary = _decode(pair["v"])
		if not k["ok"] or not v["ok"]:
			return {"ok": false, "v": null}
		obj[k["v"]] = v["v"]
	return {"ok": true, "v": obj}


func _decode_string(cps: Array) -> Dictionary:
	var s: String = ""
	for cp: Variant in cps:
		if int(cp) == 0:
			return {"ok": false, "v": null}
		s += String.chr(int(cp))
	return {"ok": true, "v": s}


func test_vectors_match_python_bytes() -> void:
	var cases: Array = _vectors().get("cases", [])
	assert_true(cases.size() > 40, "vectors missing")
	var skipped: int = 0
	for c: Dictionary in cases:
		var d: Dictionary = _decode(c["input"])
		if not d["ok"]:
			skipped += 1
			continue
		var r: RefCounted = CJson.encode(d["v"])
		assert_true(r.ok, "encode %s: %s" % [c["name"], r.message])
		if r.ok:
			assert_eq((r.value as PackedByteArray).hex_encode(), c["expected_utf8_hex"], c["name"])
	assert_true(skipped <= MAX_SKIPPED, "too many cases skipped: %d" % skipped)


func test_float_rejection_cases() -> void:
	var rejects: Array = _vectors().get("reject", [])
	assert_true(not rejects.is_empty(), "reject vectors missing")
	for c: Dictionary in rejects:
		var d: Dictionary = _decode(c["input"])
		assert_true(d["ok"], c["name"])
		assert_true(not CJson.encode(d["v"]).ok, "must reject %s" % c["name"])


func test_integral_floats_and_int_bounds() -> void:
	# Godot's JSON parser yields floats for every number.
	assert_eq((CJson.encode(3.0).value as PackedByteArray).get_string_from_utf8(), "3", "3.0")
	assert_eq((CJson.encode(-0.0).value as PackedByteArray).get_string_from_utf8(), "0", "-0.0")
	assert_eq((CJson.encode(9007199254740992.0).value as PackedByteArray).get_string_from_utf8(), "9007199254740992", "2^53")
	assert_true(not CJson.encode(9007199254740994.0).ok, "above 2^53")
	assert_true(not CJson.encode(1e300).ok, "huge")
	assert_true(not CJson.encode({1: "x"}).ok, "non-string key")
	assert_true(not CJson.encode(Vector2(1, 2)).ok, "unsupported type")


func test_key_sort_is_code_point_order() -> void:
	var d: Dictionary = {"\ue000": 1, String.chr(0x1F332): 2, "\uffee": 3, "a": 4}
	var out: String = (CJson.encode(d).value as PackedByteArray).get_string_from_utf8()
	# U+1F332 (4 UTF-8 bytes, one code point) sorts after U+E000 and U+FFEE, unlike UTF-16 order.
	assert_eq(out, "{\"a\":4,\"\ue000\":1,\"\uffee\":3,\"%s\":2}" % String.chr(0x1F332), "order")


func test_is_canonical_edge_cases() -> void:
	assert_true(CJson.is_canonical("{\"a\":1,\"b\":[true,null,\"x\"]}".to_utf8_buffer()), "canonical")
	for bad: String in ["{\"b\":1,\"a\":2}", "{\"a\": 1}", "{\"a\":1.0}", "{\"a\":1,\"a\":2}", "{\"a\":1}\n",
			"{\"a\":1e2}", " {\"a\":1}", "{\"a\":\"\\u00e9\"}", "{\"a\":1.5}", "not json", ""]:
		assert_true(not CJson.is_canonical(bad.to_utf8_buffer()), "must reject: %s" % bad.c_escape())
	assert_true(not CJson.is_canonical(PackedByteArray([0x7B, 0xEF, 0xBB, 0xBF, 0x7D])), "BOM inside / invalid")
	assert_true(not CJson.is_canonical(PackedByteArray([0x22, 0xFF, 0x22])), "invalid UTF-8")


func test_valid_lock_fixtures_are_byte_identical() -> void:
	var Lock: GDScript = load("res://addons/assetstudio/project/as_project_lock.gd")
	var files: PackedStringArray = fixture_files("locks/valid")
	assert_true(files.size() >= 2, "lock fixtures missing")
	for rel: String in files:
		var raw: PackedByteArray = fixture(rel)
		var lock: RefCounted = Lock.parse_bytes(raw)
		assert_true(lock.ok, "%s: %s" % [rel, lock.describe()])
		if lock.ok:
			assert_eq(lock.value.to_bytes().value, raw, "re-encode %s" % rel)
		assert_true(CJson.is_canonical(raw), "%s canonical" % rel)


func test_invalid_lock_fixtures_rejected_for_the_expected_reason() -> void:
	var Lock: GDScript = load("res://addons/assetstudio/project/as_project_lock.gd")
	var expected: Dictionary = {"cycle": "cycle", "key_mismatch": "does not match", "missing_closure": "missing from the lock",
			"update_policy_auto": "update_policy"}
	var files: PackedStringArray = fixture_files("locks/invalid")
	assert_eq(files.size(), expected.size(), "invalid fixture count")
	for rel: String in files:
		var raw: PackedByteArray = fixture(rel)
		var semantic: RefCounted = Lock.parse_bytes(raw, false)
		assert_true(not semantic.ok, "%s must be invalid" % rel)
		var want: String = expected.get(rel.get_file().get_basename(), "?")
		assert_true(semantic.message.contains(want), "%s: reason '%s' lacks '%s'" % [rel, semantic.message, want])
		assert_true(not Lock.parse_bytes(raw).ok, "%s also fails the strict reader" % rel)


func test_non_canonical_lock_is_rejected_by_default() -> void:
	var Lock: GDScript = load("res://addons/assetstudio/project/as_project_lock.gd")
	var raw: PackedByteArray = fixture("locks/valid/two_versions.json")
	var pretty: PackedByteArray = JSON.stringify(JSON.parse_string(raw.get_string_from_utf8()), "  ").to_utf8_buffer()
	assert_true(not Lock.parse_bytes(pretty).ok, "pretty-printed lock")
	assert_true(Lock.parse_bytes(pretty, false).ok, "same document without the canonical requirement")
