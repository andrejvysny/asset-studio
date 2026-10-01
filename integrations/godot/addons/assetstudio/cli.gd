extends SceneTree
# Headless CLI for consumer projects (design §9):
#   godot --headless --path <project> --script res://addons/assetstudio/cli.gd -- <command> [options]
# Exit codes: 0 ok, 1 failure (unavailable, integrity, unsafe, unsupported), 2 usage error.
# Credentials are read from a file (connect --token-file), never from argv, and never printed.

const Args = preload("res://addons/assetstudio/project/as_cli_args.gd")
const Commands = preload("res://addons/assetstudio/project/as_commands.gd")


func _initialize() -> void:
	# Commands await network calls, so run from the main loop rather than from _initialize.
	_run.call_deferred()


func _run() -> void:
	var parsed: RefCounted = Args.parse(OS.get_cmdline_user_args())
	if not parsed.ok:
		printerr("error: %s" % parsed.message)
		printerr(Args.USAGE)
		quit(2)
		return
	var root_dir: String = ProjectSettings.globalize_path("res://").simplify_path()
	var commands: RefCounted = Commands.new(self.root, root_dir)
	var code: int = await commands.run(parsed.value["command"], parsed.value["opts"])
	quit(code)
