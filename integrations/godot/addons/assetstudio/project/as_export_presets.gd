@tool
extends RefCounted
# Export-side checks of `export-preflight` (spec 00 §7): the project's export presets must keep AssetStudio
# bookkeeping, credentials and publish staging out of the exported game while keeping managed deliveries in, and no
# project scene/resource/autoload may reference the addon's editor or network-only scripts (an exported game never
# falls back to HTTP). Pure file inspection; each check returns [{"code", "message"}].

const Fs = preload("res://addons/assetstudio/project/as_fs.gd")

const PRESETS_FILE: String = "export_presets.cfg"
const ADDON_RES: String = "res://addons/assetstudio/"
## Paths no export may carry. Godot drops dot-prefixed directories by default; presets must still say so explicitly
## so the guarantee does not depend on engine behaviour.
const FORBIDDEN_SAMPLES: PackedStringArray = [
	".assetstudio/state.json", ".assetstudio/publish/prop/source.zip", "assetstudio.token", "server.token",
	"connections.json", "credentials.json", ".env",
]
## Addon parts that must never be reachable from an exported scene (relative to ADDON_RES).
const FORBIDDEN_REFS: PackedStringArray = [
	"editor/", "plugin.gd", "cli.gd", "project/", "core/as_library_client.gd", "core/as_stream_download.gd",
	"core/as_cancel_token.gd", "core/as_multipart.gd",
]
const MAX_SCAN_BYTES: int = 8388608


static func _p(code: String, message: String) -> Dictionary:
	return {"code": code, "message": message}


## value = {"presets": [{"name", "exclude": [..], "include": [..]}], "problems": [...]} for `wanted` (all when "").
static func load_presets(root: String, wanted: String) -> Dictionary:
	var out: Dictionary = {"presets": [], "problems": []}
	var cfg := ConfigFile.new()
	if cfg.load(root.path_join(PRESETS_FILE)) != OK:
		(out["problems"] as Array).append(_p("export_presets_missing", "%s not found or unreadable" % PRESETS_FILE))
		return out
	for section: String in cfg.get_sections():
		if not section.begins_with("preset.") or section.ends_with(".options"):
			continue
		var name: String = str(cfg.get_value(section, "name", ""))
		if wanted == "" or name == wanted:
			(out["presets"] as Array).append({"name": name, "exclude": _filters(cfg, section, "exclude_filter"),
					"include": _filters(cfg, section, "include_filter")})
	if (out["presets"] as Array).is_empty():
		(out["problems"] as Array).append(_p("export_presets_missing",
				"export preset '%s' not found in %s" % [wanted, PRESETS_FILE] if wanted != "" else "%s has no presets" % PRESETS_FILE))
	return out


static func _filters(cfg: ConfigFile, section: String, key: String) -> PackedStringArray:
	var out := PackedStringArray()
	for f: String in str(cfg.get_value(section, key, "")).split(","):
		if not f.strip_edges().is_empty():
			out.append(f.strip_edges())
	return out


## Godot's filter test: a glob against the path with and without the res:// prefix.
static func matches(path: String, filters: PackedStringArray) -> bool:
	for f: String in filters:
		if ("res://" + path).matchn(f) or path.matchn(f):
			return true
	return false


## Problems of one preset; `managed_rel` = project-relative managed root (e.g. "assets/library").
static func preset_problems(preset: Dictionary, managed_rel: String) -> Array:
	var out: Array = []
	var name: String = preset["name"]
	for sample: String in FORBIDDEN_SAMPLES:
		if not matches(sample, preset["exclude"]):
			out.append(_p("preset_missing_exclude", "preset '%s' does not exclude %s (add an exclude_filter entry)" % [name, sample]))
	for sample: String in [managed_rel + "/k/s/portable.glb", managed_rel + "/k/s/scenes/a.tscn"]:
		if matches(sample, preset["exclude"]):
			out.append(_p("preset_excludes_managed", "preset '%s' excludes managed deliveries (%s)" % [name, sample]))
	return out


## Scenes/resources/project.godot that reference editor or network-only addon scripts. value = {"files": n, "problems": []}.
static func reference_problems(root: String) -> Dictionary:
	var out: Dictionary = {"files": 0, "problems": []}
	var files: PackedStringArray = []
	_collect(root, "", files)
	files.append("project.godot")
	for rel: String in files:
		var abs_path: String = root.path_join(rel)
		if not FileAccess.file_exists(abs_path):
			continue
		out["files"] += 1
		var f: FileAccess = FileAccess.open(abs_path, FileAccess.READ)
		if f == null or f.get_length() > MAX_SCAN_BYTES:
			continue
		var text: String = f.get_as_text()
		f.close()
		for part: String in FORBIDDEN_REFS:
			if text.contains(ADDON_RES + part):
				(out["problems"] as Array).append(_p("forbidden_reference",
						"%s references the addon's editor/network-only script %s%s" % [rel, ADDON_RES, part]))
	return out


## Every .tscn/.tres outside dot-prefixed directories and the addon itself.
static func _collect(root: String, rel: String, out: PackedStringArray) -> void:
	var listing: Dictionary = Fs.list_dir(root.path_join(rel) if rel != "" else root)
	for d: String in listing["dirs"]:
		var sub: String = d if rel == "" else rel.path_join(d)
		if not d.begins_with(".") and sub != "addons/assetstudio":
			_collect(root, sub, out)
	for f: String in listing["files"]:
		if f.get_extension() == "tscn" or f.get_extension() == "tres":
			out.append(f if rel == "" else rel.path_join(f))
