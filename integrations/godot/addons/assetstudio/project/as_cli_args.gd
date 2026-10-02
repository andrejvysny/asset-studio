@tool
extends RefCounted
# Argument parsing for addons/assetstudio/cli.gd. Tokens are never accepted in argv: connect reads --token-file.

const Result = preload("res://addons/assetstudio/core/as_errors.gd")

const SPEC: Dictionary = {
	"connect": {"values": ["server-id", "url", "token-file"], "flags": ["allow-insecure-lan"],
			"required": ["server-id", "url", "token-file"]},
	"restore": {"values": [], "flags": ["locked", "offline"], "required": ["locked"]},
	"verify": {"values": [], "flags": ["locked", "offline"], "required": ["locked", "offline"]},
	"add": {"values": ["library", "asset", "version", "binding", "profile"], "flags": ["preserve"],
			"required": ["library", "asset", "version"]},
	"finalize": {"values": ["binding"], "flags": ["reapply"], "required": []},
	"set-policy": {"values": ["binding", "profile"], "flags": ["preserve", "override"], "required": ["binding"]},
	"update": {"values": ["binding", "version", "new-binding"], "flags": [], "required": ["binding", "version"]},
	"rollback": {"values": ["binding"], "flags": [], "required": ["binding"]},
}

const USAGE: String = """usage: godot --headless --path <project> --script res://addons/assetstudio/cli.gd -- <command> [options]
  connect  --server-id <uuid> --url <base_url> --token-file <path> [--allow-insecure-lan]
  restore  --locked [--offline]
  verify   --locked --offline
  add      --library <prj_..> --asset <ast_..> --version <ver_..> [--binding <id>] [--profile <id> | --preserve]
  finalize [--binding <id>] [--reapply]
  set-policy --binding <id> (--profile <profile_id> | --preserve | --override)
  update   --binding <id> --version <ver_..> [--new-binding <id>]
  rollback --binding <id>
exit codes: 0 ok, 1 failure (unavailable, integrity, unsafe, unsupported), 2 usage error"""


## ASResult whose value is {"command": String, "opts": Dictionary}; failures are usage errors (exit 2).
static func parse(args: PackedStringArray) -> RefCounted:
	if args.is_empty():
		return Result.fail("invalid_request", "missing command")
	var command: String = args[0]
	if not SPEC.has(command):
		return Result.fail("invalid_request", "unknown command: %s" % command)
	var spec: Dictionary = SPEC[command]
	var opts: Dictionary = {}
	var i: int = 1
	while i < args.size():
		var a: String = args[i]
		i += 1
		if not a.begins_with("--"):
			return Result.fail("invalid_request", "unexpected argument: %s" % a)
		var name: String = a.substr(2)
		var inline_value: String = ""
		var has_inline: bool = name.contains("=")
		if has_inline:
			inline_value = name.get_slice("=", 1)
			name = name.get_slice("=", 0)
		if (spec["flags"] as Array).has(name) and not has_inline:
			opts[name] = true
		elif (spec["values"] as Array).has(name):
			if not has_inline:
				if i >= args.size() or args[i].begins_with("--"):
					return Result.fail("invalid_request", "--%s needs a value" % name)
				inline_value = args[i]
				i += 1
			opts[name] = inline_value
		else:
			return Result.fail("invalid_request", "unknown option for %s: --%s" % [command, name])
	return _finish(command, spec, opts)


static func _finish(command: String, spec: Dictionary, opts: Dictionary) -> RefCounted:
	for r: String in spec["required"]:
		if not opts.has(r):
			return Result.fail("invalid_request", "%s: missing --%s" % [command, r])
	var chosen: int = int(opts.has("profile")) + int(opts.has("preserve")) + int(opts.has("override"))
	if chosen > 1:
		return Result.fail("invalid_request", "%s: --profile, --preserve and --override are mutually exclusive" % command)
	if command == "set-policy" and chosen == 0:
		return Result.fail("invalid_request", "set-policy: one of --profile, --preserve or --override is required")
	return Result.success({"command": command, "opts": opts})
