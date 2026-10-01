extends SceneTree
# Usage: godot --headless --path integrations/godot --script res://tests/run_tests.gd [-- --filter=<substring>]

const TESTS_DIR: String = "res://tests"


func _initialize() -> void:
	# Tests may be coroutines (network tests await); frames must keep running, so run from the main loop.
	_run.call_deferred()


func _run() -> void:
	var filter: String = ""
	for arg: String in OS.get_cmdline_user_args():
		if arg.begins_with("--filter="):
			filter = arg.substr("--filter=".length())

	var total: int = 0
	var failed: int = 0
	var files: PackedStringArray = DirAccess.get_files_at(TESTS_DIR)
	files.sort()
	for file: String in files:
		if not (file.begins_with("test_") and file.ends_with(".gd")) or file == "test_case.gd":
			continue
		var path: String = TESTS_DIR.path_join(file)
		var script: Script = load(path) as Script
		if script == null or not script.can_instantiate():
			total += 1
			failed += 1
			print("FAIL %s: script failed to load" % file)
			continue
		var methods: Array[String] = _test_methods(script)
		for method: String in methods:
			var label: String = "%s::%s" % [file, method]
			if filter != "" and not label.contains(filter):
				continue
			total += 1
			var inst: RefCounted = script.new()
			await inst.call(method)  # await is a no-op for synchronous tests
			var msgs: PackedStringArray = inst.get("failures")
			if msgs.is_empty():
				print("PASS %s" % label)
			else:
				failed += 1
				print("FAIL %s: %s" % [label, " | ".join(msgs)])
	print("TOTAL %d PASSED %d FAILED %d" % [total, total - failed, failed])
	quit(0 if failed == 0 else 1)


func _test_methods(script: Script) -> Array[String]:
	var out: Array[String] = []
	for info: Dictionary in script.get_script_method_list():
		var name: String = info["name"]
		if name.begins_with("test_"):
			out.append(name)
	out.sort()
	return out
