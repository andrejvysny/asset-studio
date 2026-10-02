@tool
extends RefCounted
# Material policies (design §7). A profile is game-owned JSON that the addon only interprets:
#   {"schema_version":1,"profile_id":"<slug>","rules":[{"match":{"slot_id":..,"role":..},"material":"res://x.tres"}
#                                                      |{"match":{..},"patch":{"metallic":0.0,..}}]}
# Parsing is strict (unknown keys, bad types, unsafe paths and unloadable materials are errors). The first rule
# whose match fits a descriptor slot wins; a slot no rule matches keeps its source material and is reported.
# profile_sha256 is the sha256 of the raw file bytes. `override` mode reads
# <profiles_dir>/overrides/<binding_id>.json in the same format (its profile_id must equal the binding id).

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Schema = preload("res://addons/assetstudio/core/as_schema.gd")
const Fs = preload("res://addons/assetstudio/project/as_fs.gd")

const MAX_RULES: int = 256
const PATCH_KEYS: PackedStringArray = ["metallic", "metallic_texture", "roughness", "cull_mode",
		"vertex_color_use_as_albedo", "albedo_color", "transparency", "alpha_scissor_threshold"]
const UNIT_KEYS: PackedStringArray = ["metallic", "roughness", "alpha_scissor_threshold"]


## Absolute path of the profile file. `mode` is "project_mapping" (needs `profile_id`) or "override".
static func profile_file(root: String, config: RefCounted, mode: String, profile_id: String, binding_id: String) -> String:
	var dir: String = Fs.res_to_abs(root, config.get("material_profiles_dir"))
	if mode == "override":
		return dir.path_join("overrides").path_join("%s.json" % binding_id)
	return dir.path_join("%s.json" % profile_id)


## value = {"profile_id", "sha256", "rules": [{"match": {..}, "material": Material|null, "material_path": String,
## "patch": Dictionary}]}. `load_materials` = false skips loading the referenced Material resources.
static func parse(raw: PackedByteArray, expected_id: String, load_materials: bool = true) -> RefCounted:
	var parsed: Dictionary = Schema.parse_json_bytes(raw)
	if not parsed["ok"]:
		return Result.fail(Result.CODE_INVALID_PROJECT_FILE, "material profile: %s" % parsed["error"])
	var d: Dictionary = parsed["value"]
	var err: String = Schema.check_keys(d, ["schema_version", "profile_id", "rules"], PackedStringArray(), "profile")
	if err == "":
		err = Schema.check_int(d["schema_version"], 1, 1, "schema_version")
	if err == "":
		err = Schema.check_pattern(d["profile_id"], "slug", "profile_id")
	if err == "" and d["profile_id"] != expected_id:
		err = "profile_id %s does not match %s" % [d["profile_id"], expected_id]
	if err == "" and (not d["rules"] is Array or (d["rules"] as Array).size() > MAX_RULES):
		err = "rules: expected array of at most %d" % MAX_RULES
	var rules: Array = []
	if err == "":
		for i: int in (d["rules"] as Array).size():
			var rule: RefCounted = _parse_rule(d["rules"][i], i, load_materials)
			if not rule.ok:
				return rule
			rules.append(rule.value)
	if err != "":
		return Result.fail(Result.CODE_INVALID_PROJECT_FILE, "material profile: %s" % err)
	return Result.success({"profile_id": d["profile_id"], "sha256": Fs.sha256_bytes(raw), "rules": rules})


static func _parse_rule(v: Variant, index: int, load_materials: bool) -> RefCounted:
	var label: String = "rules[%d]" % index
	if not v is Dictionary:
		return _bad("%s: expected object" % label)
	var rule: Dictionary = v
	var err: String = Schema.check_keys(rule, ["match"], ["material", "patch"], label)
	if err == "" and rule.has("material") == rule.has("patch"):
		err = "%s: exactly one of material or patch" % label
	if err == "":
		err = _check_match(rule["match"], label)
	if err != "":
		return _bad(err)
	var out: Dictionary = {"match": rule["match"], "material": null, "material_path": "", "patch": {}}
	if rule.has("material"):
		var mat: RefCounted = _load_material(rule["material"], label, load_materials)
		if not mat.ok:
			return mat
		out["material_path"] = rule["material"]
		out["material"] = mat.value
	else:
		err = _check_patch(rule["patch"], label)
		if err != "":
			return _bad(err)
		out["patch"] = rule["patch"]
	return Result.success(out)


static func _bad(msg: String) -> RefCounted:
	return Result.fail(Result.CODE_INVALID_PROJECT_FILE, "material profile: %s" % msg)


static func _check_match(m: Variant, label: String) -> String:
	if not m is Dictionary or (m as Dictionary).is_empty():
		return "%s.match: expected non-empty object" % label
	var err: String = Schema.check_keys(m, PackedStringArray(), ["slot_id", "role"], label + ".match")
	if err != "":
		return err
	for k: String in (m as Dictionary).keys():
		err = Schema.check_pattern(m[k], "slug", "%s.match.%s" % [label, k])
		if err != "":
			return err
	return ""


static func _load_material(path: Variant, label: String, load_materials: bool) -> RefCounted:
	if not path is String or not Fs.is_safe_res_path(path):
		return _bad("%s.material: must be a safe res:// path" % label)
	if not load_materials:
		return Result.success(null)
	if not ResourceLoader.exists(path):
		return _bad("%s.material: %s does not exist" % [label, path])
	var res: Resource = load(path)
	if not res is Material:
		return _bad("%s.material: %s is not a Material" % [label, path])
	return Result.success(res)


static func _check_patch(p: Variant, label: String) -> String:
	if not p is Dictionary or (p as Dictionary).is_empty():
		return "%s.patch: expected non-empty object" % label
	for k: Variant in (p as Dictionary).keys():
		if not k is String or not PATCH_KEYS.has(k):
			return "%s.patch: key %s is not allowed" % [label, str(k)]
		var err: String = _check_patch_value(k, p[k], "%s.patch.%s" % [label, k])
		if err != "":
			return err
	return ""


static func _check_patch_value(key: String, v: Variant, field: String) -> String:
	if key == "metallic_texture":
		return "" if v == null else "%s: only null is allowed" % field
	if UNIT_KEYS.has(key):
		return "" if (v is float or v is int) and float(v) >= 0.0 and float(v) <= 1.0 else "%s: expected 0..1" % field
	match key:
		"cull_mode":
			return Schema.check_int(v, 0, 2, field)
		"transparency":
			return Schema.check_int(v, 0, 5, field)
		"vertex_color_use_as_albedo":
			return "" if v is bool else "%s: expected boolean" % field
	return "" if _color_of(v) != null else "%s: expected #rrggbb[aa] or [r,g,b(,a)] in 0..1" % field


static func _color_of(v: Variant) -> Variant:
	if v is String and Color.html_is_valid(v):
		return Color.html(v)
	if v is Array and ((v as Array).size() == 3 or (v as Array).size() == 4):
		for c: Variant in v:
			if not (c is float or c is int) or float(c) < 0.0 or float(c) > 1.0:
				return null
		return Color(float(v[0]), float(v[1]), float(v[2]), float(v[3]) if (v as Array).size() == 4 else 1.0)
	return null


## First matching rule index per descriptor slot: [{"slot_id", "role", "rule": int (-1 = unmatched)}].
static func evaluate(rules: Array, slots: Array) -> Array:
	var out: Array = []
	for s: Variant in slots:
		var slot: Dictionary = s
		var hit: int = -1
		for i: int in rules.size():
			var m: Dictionary = rules[i]["match"]
			if (not m.has("slot_id") or m["slot_id"] == slot["slot_id"]) and (not m.has("role") or m["role"] == slot["role"]):
				hit = i
				break
		out.append({"slot_id": slot["slot_id"], "role": slot["role"], "rule": hit})
	return out


## Writes surface overrides into `model_root` (the instantiated imported scene). `mapping` = slot resolver
## "slots". value = {"overrides": int, "unmapped": [slot_id] (no rule or no resolved surface)}.
static func apply(model_root: Node, rules: Array, slots: Array, mapping: Dictionary) -> RefCounted:
	var count: int = 0
	var unmapped: Array = []
	for hit: Dictionary in evaluate(rules, slots):
		var targets: Array = mapping.get(hit["slot_id"], [])
		if hit["rule"] < 0 or targets.is_empty():
			unmapped.append(hit["slot_id"])
			continue
		var rule: Dictionary = rules[hit["rule"]]
		for t: Dictionary in targets:
			var node: MeshInstance3D = model_root.get_node_or_null(NodePath(t["path"])) as MeshInstance3D
			if node == null:
				return Result.fail("invalid_request", "slot %s: node %s vanished" % [hit["slot_id"], t["path"]])
			var mat: RefCounted = _override_for(node, int(t["surface"]), rule, "%s_%d" % [hit["slot_id"], count])
			if not mat.ok:
				return mat
			node.set_surface_override_material(int(t["surface"]), mat.value)
			count += 1
	return Result.success({"overrides": count, "unmapped": unmapped})


static func _override_for(node: MeshInstance3D, surface: int, rule: Dictionary, uid: String) -> RefCounted:
	if rule["material"] != null:
		return Result.success(rule["material"])
	var src: Material = node.mesh.surface_get_material(surface)
	var mat: BaseMaterial3D = null
	if src == null:
		mat = StandardMaterial3D.new()
	elif src is BaseMaterial3D:
		mat = (src as BaseMaterial3D).duplicate() as BaseMaterial3D
	else:
		return Result.fail("invalid_request", "cannot patch %s: its source material is not a BaseMaterial3D" % node.name)
	for k: String in rule["patch"]:
		var v: Variant = rule["patch"][k]
		if k == "albedo_color":
			v = _color_of(v)
		elif k == "metallic_texture":
			v = null
		elif k in ["cull_mode", "transparency"]:
			v = int(v)
		mat.set(k, v)
	mat.resource_scene_unique_id = "patched_%s" % uid
	return Result.success(mat)
