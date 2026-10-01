extends "res://tests/test_case.gd"

const Registry = preload("res://addons/assetstudio/core/as_connection_registry.gd")

const SID: String = "6f1c2a52-3c2e-4d4b-9a57-0b6f6f0c1d2e"


func _dir() -> String:
	return "user://as_test_registry_%d_%d" % [Time.get_ticks_usec(), randi()]


func _cleanup(dir: String) -> void:
	for f: String in DirAccess.get_files_at(dir):
		DirAccess.remove_absolute(dir.path_join(f))
	DirAccess.remove_absolute(dir)


func test_url_rules() -> void:
	for good: String in ["https://studio.example.com", "https://studio.example.com:8443/", "http://127.0.0.1:8192",
			"http://localhost:8192", "http://[::1]:8192", "HTTPS://Studio.Example.com"]:
		assert_true(Registry.parse_base_url(good, false).ok, "accept %s" % good)
	for bad: String in ["", "ftp://x.com", "https://x.com/api", "https://x.com/?a=1", "https://user:pw@x.com",
			"https://x.com#f", "x.com", "https://x.com:99999", "https:///x", "https://x .com", "https://x.com\n"]:
		assert_true(not Registry.parse_base_url(bad, true).ok, "reject %s" % bad.c_escape())


func test_cleartext_lan_requires_opt_in() -> void:
	var refused: RefCounted = Registry.parse_base_url("http://192.168.1.20:8192", false)
	assert_true(not refused.ok, "refused without opt-in")
	assert_eq(refused.code, "invalid_request", "typed error")
	assert_true(Registry.parse_base_url("http://192.168.1.20:8192", true).ok, "allowed with opt-in")
	assert_true(Registry.parse_base_url("https://192.168.1.20:8192", false).ok, "https never needs opt-in")


func test_credentials_stored_separately_and_persist() -> void:
	var dir: String = _dir()
	var reg: RefCounted = Registry.new(dir)
	assert_true(reg.set_connection(SID, "http://127.0.0.1:8192").ok, "set_connection")
	assert_true(reg.set_credential(SID, "asi_secret_token").ok, "set_credential")
	var conns: String = FileAccess.get_file_as_string(dir.path_join("connections.json"))
	assert_true(not conns.contains("asi_secret_token"), "token not in connections file")
	assert_true(FileAccess.get_file_as_string(dir.path_join("credentials.json")).contains("asi_secret_token"), "token in credentials file")
	var again: RefCounted = Registry.new(dir)
	assert_eq(again.get_connection(SID).value["base_url"], "http://127.0.0.1:8192", "persisted url")
	assert_eq(again.credential_for_request(SID), "asi_secret_token", "persisted credential")
	assert_true(not again.get_connection("11111111-2222-4333-8444-555555555555").ok, "unknown server")
	_cleanup(dir)


func test_rejects_bad_server_id_and_tampered_config() -> void:
	var dir: String = _dir()
	var reg: RefCounted = Registry.new(dir)
	assert_true(not reg.set_connection("not-a-uuid", "https://x.com").ok, "bad server id")
	assert_true(not reg.set_connection(SID, "http://10.0.0.5:1").ok, "lan without opt-in")
	reg.set_connection(SID, "https://x.com")
	var f: FileAccess = FileAccess.open(dir.path_join("connections.json"), FileAccess.WRITE)
	f.store_string(JSON.stringify({"connections": {SID: {"base_url": "http://10.0.0.5:1", "allow_insecure_lan": false}}}))
	f.close()
	assert_true(not Registry.new(dir).get_connection(SID).ok, "edited config is re-validated")
	_cleanup(dir)
