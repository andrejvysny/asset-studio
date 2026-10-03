extends "res://tests/client_test_base.gd"
# Publication calls of the client against fake_server.py: multipart preview, commit with idempotency key and
# compare-and-swap, operation query, and the "never auto-retry" rules.

const KEY_A: String = "asp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
const KEY_B: String = "asp_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


func _parts(extra: Dictionary = {}) -> Dictionary:
	var draft: Dictionary = {"schema_version": 1, "material_slots": [], "preview_warnings": ["custom_shader_approximated"]}
	var parts: Dictionary = {
		"portable": {"bytes": "glTF-fake-glb".to_utf8_buffer(), "filename": "portable.glb", "media_type": "model/gltf-binary"},
		"descriptor": {"bytes": JSON.stringify(draft).to_utf8_buffer(), "filename": "descriptor.json", "media_type": "application/json"}}
	parts.merge(extra, true)
	return parts


func _body(receipt: Dictionary, key: String, extra: Dictionary = {}) -> Dictionary:
	var body: Dictionary = {"preview_id": receipt["preview_id"], "portable_sha256": receipt["portable_sha256"],
			"descriptor_draft_sha256": receipt["descriptor_draft_sha256"], "package_sha256": receipt["package_sha256"],
			"name": "Crate", "idempotency_key": key, "target_asset_id": null, "expected_current_version": null}
	body.merge(extra, true)
	return body


func _preview(client: Node) -> RefCounted:
	return await client.publication_preview(LIBRARY, _parts())


func _published() -> Array:
	return await control(HTTPClient.METHOD_GET, "/__publications")


func test_client_publication_preview_returns_a_receipt() -> void:
	if not has_server("preview"):
		return
	var env: Dictionary = make_env()
	var r: RefCounted = await _preview(env["client"])
	assert_true(r.ok, "preview: %s" % r.describe())
	if r.ok:
		assert_true(str(r.value["preview_id"]).begins_with("ipv_"), "preview id")
		assert_eq(r.value["portable_sha256"], Canonical.sha256_hex("glTF-fake-glb".to_utf8_buffer()), "server hashed what was sent")
		assert_eq(r.value["warnings"], ["custom_shader_approximated"], "draft warnings are echoed")
	var bad: RefCounted = await env["client"].publication_preview(LIBRARY, {"portable": _parts()["portable"]})
	assert_eq(bad.code, "invalid_request", "a missing descriptor part is refused")
	await finish()


func test_client_publication_commit_is_idempotent_and_key_bound() -> void:
	if not has_server("commit"):
		return
	await control(HTTPClient.METHOD_POST, "/__publish_reset")
	var env: Dictionary = make_env()
	var client: Node = env["client"]
	var receipt: Dictionary = (await _preview(client)).value
	var first: RefCounted = await client.publication_commit(LIBRARY, _body(receipt, KEY_A))
	assert_true(first.ok, "commit: %s" % first.describe())
	var replay: RefCounted = await client.publication_commit(LIBRARY, _body(receipt, KEY_A))
	assert_true(replay.ok and replay.value["asset_ref"] == first.value["asset_ref"], "same key and body: same result")
	assert_eq((await _published()).size(), 1, "no duplicate publication")
	var changed: RefCounted = await client.publication_commit(LIBRARY, _body(receipt, KEY_A, {"name": "Other"}))
	assert_eq(changed.code, "idempotency_conflict", "same key, different request")
	var op: RefCounted = await client.publication_operation(LIBRARY, KEY_A)
	assert_true(op.ok and op.value["state"] == "committed" and op.value["version_id"] == first.value["asset_ref"]["version_id"], "operation query")
	var unknown: RefCounted = await client.publication_operation(LIBRARY, KEY_B)
	assert_true(unknown.ok and unknown.value["state"] == "unknown", "unknown key")
	await control(HTTPClient.METHOD_POST, "/__publish_reset")
	await finish()


func test_client_publication_compare_and_swap() -> void:
	if not has_server("cas"):
		return
	await control(HTTPClient.METHOD_POST, "/__publish_reset")
	var env: Dictionary = make_env()
	var client: Node = env["client"]
	var receipt: Dictionary = (await _preview(client)).value
	await clear_log()
	var half: RefCounted = await client.publication_commit(LIBRARY, _body(receipt, KEY_A, {"target_asset_id": ASSET}))
	assert_eq(half.code, "invalid_request", "target without expected version")
	assert_true((await server_log()).is_empty(), "refused locally, no request")
	var ok: RefCounted = await client.publication_commit(LIBRARY, _body(receipt, KEY_A, {"target_asset_id": ASSET, "expected_current_version": V2}))
	assert_true(ok.ok and ok.value["asset_ref"]["asset_id"] == ASSET, "new version with the expected base: %s" % ok.describe())
	var stale: RefCounted = await client.publication_commit(LIBRARY, _body(receipt, KEY_B, {"target_asset_id": ASSET, "expected_current_version": V2}))
	assert_eq(stale.code, "stale_pointer", "stale base version")
	assert_eq(stale.details.get("current_version_id"), ok.value["asset_ref"]["version_id"], "the current version is reported")
	assert_eq((await _published()).size(), 1, "the stale commit published nothing")
	await control(HTTPClient.METHOD_POST, "/__publish_reset")
	await finish()


func test_client_publication_never_retries_uploads_or_commits() -> void:
	if not has_server("no retries"):
		return
	await control(HTTPClient.METHOD_POST, "/__publish_reset")
	var env: Dictionary = make_env()
	var client: Node = env["client"]
	var receipt: Dictionary = (await _preview(client)).value
	await set_scenario("preview_busy")
	await clear_log()
	var busy: RefCounted = await _preview(client)
	assert_eq(busy.code, "temporarily_unavailable", "admission refusal")
	assert_eq(busy.details.get("reason"), "staging_capacity", "reason is passed through")
	assert_eq(requests_to(await server_log(), "publications:preview").size(), 1, "the upload was not retried")
	await set_scenario("drop_commit_response")
	await clear_log()
	var lost: RefCounted = await client.publication_commit(LIBRARY, _body(receipt, KEY_A))
	assert_eq(lost.code, "network_error", "lost response")
	assert_eq(requests_to(await server_log(), "publications:commit").size(), 1, "the commit was not auto-retried")
	await set_scenario("normal")
	var op: RefCounted = await client.publication_operation(LIBRARY, KEY_A)
	assert_true(op.ok and op.value["state"] == "committed", "the commit did reach the server")
	assert_eq((await _published()).size(), 1, "exactly one publication")
	await control(HTTPClient.METHOD_POST, "/__publish_reset")
	await finish()


func test_client_publication_validates_keys_and_ids_locally() -> void:
	if not has_server("local validation"):
		return
	var env: Dictionary = make_env()
	await clear_log()
	var client: Node = env["client"]
	assert_eq((await client.publication_commit(LIBRARY, {"idempotency_key": "short"})).code, "invalid_request", "short key")
	assert_eq((await client.publication_operation(LIBRARY, "../etc/passwd")).code, "invalid_request", "unsafe key")
	assert_eq((await client.publication_preview("../x", _parts())).code, "invalid_request", "unsafe library")
	assert_true((await server_log()).is_empty(), "no request was sent")
	await finish()
