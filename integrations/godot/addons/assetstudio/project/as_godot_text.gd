@tool
extends RefCounted
# Bounded, inert parser/serializer for the Godot text-resource subset (.tscn/.tres, format=3|4; grammar
# static-source-package.md §3). Nothing is evaluated or instantiated: values become plain data nodes.
#
# The file is split into statements (a header "[...]", a property "key = value", or a pass-through blank/comment
# line). A statement can span lines (multi-line strings, arrays). Every statement keeps its original text in
# "raw"; a writer re-serializes only the headers it changes, so untouched statements are byte-identical.
#
# Value nodes: {"t": "str", "v"} {"t": "num", "v": text} {"t": "lit", "v": "true"|"false"|"null"}
# {"t": "ref", "kind": "ExtResource"|"SubResource", "id"} {"t": "call", "name", "args": []}
# {"t": "arr", "items": []} {"t": "dict", "items": [[key, value], ...]}

const Result = preload("res://addons/assetstudio/core/as_errors.gd")
const Cur = preload("res://addons/assetstudio/project/as_godot_cursor.gd")

const MAX_INPUT_BYTES: int = 16 * 1024 * 1024
const MAX_STATEMENTS: int = 100000
const BINARY_MAGICS: PackedStringArray = ["RSRC", "GDSC", "GDRC", "RSCC"]


static func _fail(code: String, detail: String, message: String, line: int = 0) -> RefCounted:
	return Result.fail(code, message if line == 0 else "%s (line %d)" % [message, line], false,
			{"detail": detail, "line": line})


## value = {"kind": "gd_scene" | "gd_resource", "statements": Array[Dictionary]}.
static func parse_bytes(data: PackedByteArray) -> RefCounted:
	if data.size() > MAX_INPUT_BYTES:
		return _fail("resource_limit", "input_limit", "text resource larger than %d bytes" % MAX_INPUT_BYTES)
	if data.size() >= 4 and BINARY_MAGICS.has(data.slice(0, 4).get_string_from_ascii()):
		return _fail("unsafe_package", "binary_resource", "binary resource is not Godot text format", 1)
	var text: String = data.get_string_from_utf8()
	if text.to_utf8_buffer() != data:
		return _fail("unsafe_package", "not_utf8", "not valid UTF-8", 1)
	return parse_text(text)


static func parse_text(text: String) -> RefCounted:
	if text.begins_with("﻿"):
		text = text.substr(1)
	var lines: PackedStringArray = text.split("\n")
	var statements: Array = []
	var budget: Dictionary = {"tokens": 0}
	var i: int = 0
	while i < lines.size():
		var stripped: String = lines[i].strip_edges()
		if stripped.is_empty() or stripped.begins_with(";"):
			statements.append({"kind": "pass", "raw": lines[i]})
			i += 1
			continue
		if statements.size() >= MAX_STATEMENTS:
			return _fail("resource_limit", "section_limit", "more than %d statements" % MAX_STATEMENTS, i + 1)
		var gathered: Dictionary = _gather(lines, i)
		if gathered.has("error"):
			return gathered["error"]
		var stmt: Dictionary = _parse_statement(gathered["raw"], i + 1, budget)
		if stmt.has("error"):
			return stmt["error"]
		statements.append(stmt)
		i = gathered["next"]
	return _finish(statements)


## The first real statement must be the [gd_scene]/[gd_resource] header with format=3 or 4 and no properties.
static func _finish(statements: Array) -> RefCounted:
	var first: int = -1
	for k: int in statements.size():
		if statements[k]["kind"] != "pass":
			first = k
			break
	if first < 0 or statements[first]["kind"] != "header" or not ["gd_scene", "gd_resource"].has(statements[first]["section"]):
		return _fail("unsafe_package", "parse_error", "file must start with [gd_scene] or [gd_resource]", 1)
	var root: Dictionary = statements[first]
	for k: int in range(first + 1, statements.size()):
		if statements[k]["kind"] == "prop":
			return _fail("unsafe_package", "parse_error", "properties after the file header", int(statements[k]["line"]))
		if statements[k]["kind"] == "header":
			break
	var fmt: Variant = attr(root, "format")
	if fmt == null or fmt["t"] != "num" or not ["3", "4"].has(fmt["v"]):
		return _fail("unsafe_package", "unsupported_format", "only text format 3 or 4 is supported", int(root["line"]))
	return Result.success({"kind": root["section"], "statements": statements, "root_index": first})


## Collects the lines of one statement: nesting and strings must close. {"raw", "next"} or {"error"}.
static func _gather(lines: PackedStringArray, start: int) -> Dictionary:
	var st: Dictionary = {"depth": 0, "in_str": false, "esc": false}
	var raw: String = lines[start]
	var i: int = start
	while true:
		if not _scan_line(lines[i], st):
			return {"error": _fail("unsafe_package", "parse_error", "unbalanced brackets", i + 1)}
		if st["depth"] == 0 and not st["in_str"]:
			return {"raw": raw, "next": i + 1}
		i += 1
		if i >= lines.size():
			return {"error": _fail("unsafe_package", "parse_error", "unterminated value", start + 1)}
		raw += "\n" + lines[i]
	return {}


static func _scan_line(line: String, st: Dictionary) -> bool:
	for i: int in line.length():
		var c: int = line.unicode_at(i)
		if st["in_str"]:
			if st["esc"]:
				st["esc"] = false
			elif c == 92:
				st["esc"] = true
			elif c == 34:
				st["in_str"] = false
		elif c == 34:
			st["in_str"] = true
		elif c == 59:
			return true
		elif c == 91 or c == 40 or c == 123:
			st["depth"] += 1
		elif c == 93 or c == 41 or c == 125:
			st["depth"] -= 1
			if st["depth"] < 0:
				return false
	return true


static func _parse_statement(raw: String, line: int, budget: Dictionary) -> Dictionary:
	var cur: Cur = Cur.new(raw, line, budget)
	var stmt: Dictionary = cur.header() if raw.strip_edges(true, false).begins_with("[") else cur.property()
	if cur.err != "":
		return {"error": _fail(cur.code, cur.detail, cur.err, cur.line_at())}
	stmt["raw"] = raw
	stmt["line"] = line
	return stmt


# --- helpers for consumers ----------------------------------------------------------------------------------

## Attribute value node of a header statement, or null.
static func attr(stmt: Dictionary, name: String) -> Variant:
	for a: Dictionary in stmt.get("attrs", []):
		if a["name"] == name:
			return a["node"]
	return null


static func str_attr(stmt: Dictionary, name: String) -> String:
	var n: Variant = attr(stmt, name)
	return n["v"] if n != null and n["t"] == "str" else ""


## Appends every Ref node of `value` to `refs`, every Call node to `calls` and every str node to `strs`.
static func collect(value: Variant, refs: Array, calls: Array, strs: Array) -> void:
	var stack: Array = [value]
	while not stack.is_empty():
		var v: Dictionary = stack.pop_back()
		match v["t"]:
			"ref":
				refs.append(v)
			"str":
				strs.append(v)
			"call":
				calls.append(v)
				stack.append_array(v["args"])
			"arr":
				stack.append_array(v["items"])
			"dict":
				for kv: Array in v["items"]:
					stack.append(kv[0])
					stack.append(kv[1])


static func quote(s: String) -> String:
	return "\"%s\"" % s.replace("\\", "\\\\").replace("\"", "\\\"")


## Header text from the section kind and [{"name", "raw"}] attributes (raw value text is copied verbatim).
static func header_text(section: String, attrs: Array) -> String:
	var parts: PackedStringArray = [section]
	for a: Dictionary in attrs:
		parts.append("%s=%s" % [a["name"], a["raw"]])
	return "[%s]" % " ".join(parts)


## `#include` line of a shader. {} when the line is not an include directive; else {"ok": bool, "arg", "head",
## "tail"} where head + quote(arg) + tail rebuilds the line.
static func parse_include(line: String) -> Dictionary:
	var rest: String = line.lstrip(" \t")
	if not rest.begins_with("#"):
		return {}
	rest = rest.substr(1).lstrip(" \t")
	if not rest.begins_with("include") or (rest.length() > 7 and _is_word(rest.unicode_at(7))):
		return {}
	var after: String = rest.substr(7)
	var open: int = after.find("\"")
	var close: int = after.find("\"", open + 1) if open >= 0 else -1
	if open < 0 or close < 0 or not after.substr(0, open).strip_edges().is_empty():
		return {"ok": false}
	var tail: String = after.substr(close + 1)
	if not (tail.strip_edges().is_empty() or tail.strip_edges().begins_with("//")):
		return {"ok": false}
	var head_len: int = line.length() - after.length() + open
	return {"ok": true, "arg": after.substr(open + 1, close - open - 1), "head": line.substr(0, head_len), "tail": tail}


static func _is_word(c: int) -> bool:
	return (c >= 48 and c <= 57) or (c >= 65 and c <= 90) or (c >= 97 and c <= 122) or c == 95
