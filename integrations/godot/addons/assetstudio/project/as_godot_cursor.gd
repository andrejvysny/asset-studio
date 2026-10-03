@tool
extends RefCounted
# Value/statement cursor of the Godot text-resource parser (see as_godot_text.gd). One instance parses one
# statement. Errors are recorded in err/code/detail (no exceptions in GDScript).

const MAX_DEPTH: int = 64
const MAX_TOKENS: int = 2000000
const MAX_STRING: int = 1024 * 1024
const SPECIAL_FLOATS: PackedStringArray = ["inf", "nan", "inf_neg"]
const ESCAPES: Dictionary = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "a": "\a", "v": "\v"}

var s: String
var p: int = 0
var n: int
var start_line: int
var budget: Dictionary
var err: String = ""
var code: String = "unsafe_package"
var detail: String = "parse_error"

func _init(text: String, line: int, shared_budget: Dictionary) -> void:
	s = text
	n = text.length()
	start_line = line
	budget = shared_budget

func line_at() -> int:
	return start_line + s.count("\n", 0, mini(p, n))

func bad(msg: String) -> Dictionary:
	if err == "":
		err = msg
	return {}

func tick() -> void:
	budget["tokens"] += 1
	if budget["tokens"] > MAX_TOKENS and err == "":
		err = "more than %d tokens" % MAX_TOKENS
		code = "resource_limit"
		detail = "token_limit"

func ws() -> void:
	while p < n:
		var c: int = s.unicode_at(p)
		if c == 32 or c == 9 or c == 10 or c == 13 or c == 11 or c == 12:
			p += 1
		elif c == 59:
			var e: int = s.find("\n", p)
			p = n if e < 0 else e
		else:
			break

func peek() -> int:
	ws()
	return s.unicode_at(p) if p < n else -1

func expect(c: int) -> bool:
	if peek() != c:
		bad("expected %s" % String.chr(c))
		return false
	p += 1
	tick()
	return true

func ident() -> String:
	var st: int = p
	if p < n and (_is_word(s.unicode_at(p)) and not (s.unicode_at(p) >= 48 and s.unicode_at(p) <= 57)):
		p += 1
		while p < n and _is_word(s.unicode_at(p)):
			p += 1
	tick()
	return s.substr(st, p - st)

func end_of_statement() -> void:
	ws()
	if p < n and err == "":
		bad("unexpected text after value")

func header() -> Dictionary:
	ws()
	p += 1
	ws()
	var section: String = ident()
	if section.is_empty():
		return bad("expected a section kind")
	var attrs: Array = []
	var seen: Dictionary = {}
	while err == "" and peek() != 93:
		if p >= n:
			return bad("unterminated header")
		var name: String = ident()
		if name.is_empty() or seen.has(name):
			return bad("expected a unique attribute name")
		seen[name] = true
		if not expect(61):
			return {}
		ws()
		var st: int = p
		var node: Dictionary = value(0)
		if err != "":
			return {}
		attrs.append({"name": name, "node": node, "raw": s.substr(st, p - st)})
	if err != "":
		return {}
	p += 1
	end_of_statement()
	return {"kind": "header", "section": section, "attrs": attrs}

func property() -> Dictionary:
	ws()
	var st: int = p
	if p >= n or not (_is_word(s.unicode_at(p)) and not (s.unicode_at(p) >= 48 and s.unicode_at(p) <= 57)):
		return bad("expected a property name")
	while p < n and not " \t\r\n=;[]{}(),\"".contains(s[p]):
		p += 1
	var key: String = s.substr(st, p - st)
	tick()
	if not expect(61):
		return {}
	var node: Dictionary = value(0)
	if err != "":
		return {}
	end_of_statement()
	return {"kind": "prop", "key": key, "value": node}

func value(depth: int) -> Dictionary:
	tick()
	var c: int = peek()
	if c < 0:
		return bad("unexpected end of input")
	if c == 34:
		return string_node("str")
	if (c == 38 or c == 94) and p + 1 < n and s.unicode_at(p + 1) == 34:
		p += 1
		var inner: Dictionary = string_node("str")
		return {"t": "call", "name": "StringName" if c == 38 else "NodePath", "args": [inner]}
	if c == 91:
		return array(depth + 1)
	if c == 123:
		return dict(depth + 1)
	if c == 43 or c == 45 or c == 46 or (c >= 48 and c <= 57):
		return number()
	return word(depth)

func enter(depth: int) -> bool:
	if depth > MAX_DEPTH:
		bad("nesting deeper than %d" % MAX_DEPTH)
		code = "resource_limit"
		detail = "depth_limit"
		return false
	return true

func number() -> Dictionary:
	var st: int = p
	if s.unicode_at(p) == 43 or s.unicode_at(p) == 45:
		p += 1
	if p < n and _is_word(s.unicode_at(p)) and not _digit(s.unicode_at(p)) and s.unicode_at(st) == 45:
		var word_start: int = p
		var w: String = ident()
		if SPECIAL_FLOATS.has(w):
			return {"t": "num", "v": "-" + w}
		p = word_start
		return bad("malformed number")
	if p + 1 < n and s.unicode_at(p) == 48 and (s[p + 1] == "x" or s[p + 1] == "X"):
		p += 2
		while p < n and s[p].is_valid_hex_number(false):
			p += 1
	else:
		digits()
		if p < n and s.unicode_at(p) == 46:
			p += 1
			digits()
		if p < n and (s[p] == "e" or s[p] == "E"):
			p += 1
			if p < n and (s[p] == "+" or s[p] == "-"):
				p += 1
			if digits() == 0:
				return bad("malformed number")
	var text: String = s.substr(st, p - st)
	if text.length() == 0 or text == "+" or text == "-" or text == "." or (p < n and _is_word(s.unicode_at(p))):
		return bad("malformed number")
	return {"t": "num", "v": text}

func digits() -> int:
	var st: int = p
	while p < n and _digit(s.unicode_at(p)):
		p += 1
	return p - st

func _digit(c: int) -> bool:
	return c >= 48 and c <= 57

func word(depth: int) -> Dictionary:
	var name: String = ident()
	if name.is_empty():
		return bad("unexpected character")
	if SPECIAL_FLOATS.has(name):
		return {"t": "num", "v": name}
	if name == "true" or name == "false" or name == "null":
		return {"t": "lit", "v": name}
	if (name == "Array" or name == "Dictionary") and p < n and s.unicode_at(p) == 91:
		var close: int = s.find("]", p)
		if close < 0:
			return bad("unterminated type argument")
		name += s.substr(p, close + 1 - p)
		p = close + 1
	if peek() != 40:
		return bad("unexpected identifier %s" % name)
	return call_node(name, depth + 1)

func call_node(name: String, depth: int) -> Dictionary:
	if not enter(depth):
		return {}
	p += 1
	var args: Array = []
	while err == "" and peek() != 41:
		if p >= n:
			return bad("unterminated call")
		if not args.is_empty() and not expect(44):
			return {}
		args.append(value(depth))
	if err != "":
		return {}
	p += 1
	tick()
	if name == "ExtResource" or name == "SubResource":
		if args.size() != 1 or not (args[0]["t"] == "str" or args[0]["t"] == "num"):
			return bad("%s takes one id" % name)
		return {"t": "ref", "kind": name, "id": args[0]["v"]}
	return {"t": "call", "name": name, "args": args}

func array(depth: int) -> Dictionary:
	if not enter(depth):
		return {}
	p += 1
	var items: Array = []
	while err == "" and peek() != 93:
		if p >= n:
			return bad("unterminated array")
		if not items.is_empty() and not expect(44):
			return {}
		items.append(value(depth))
	if err != "":
		return {}
	p += 1
	tick()
	return {"t": "arr", "items": items}

func dict(depth: int) -> Dictionary:
	if not enter(depth):
		return {}
	p += 1
	var items: Array = []
	while err == "" and peek() != 125:
		if p >= n:
			return bad("unterminated dictionary")
		if not items.is_empty() and not expect(44):
			return {}
		var k: Dictionary = value(depth)
		if err == "" and (k["t"] == "arr" or k["t"] == "dict"):
			return bad("unhashable dictionary key")
		if err != "" or not expect(58):
			return {}
		items.append([k, value(depth)])
	if err != "":
		return {}
	p += 1
	tick()
	return {"t": "dict", "items": items}

func string_node(kind: String) -> Dictionary:
	var start: int = p
	p += 1
	var out := PackedStringArray()
	var size: int = 0
	while true:
		var q: int = s.find("\"", p)
		var b: int = s.find("\\", p)
		if q < 0:
			p = start
			return bad("unterminated string")
		var stop: int = q if b < 0 or q < b else b
		out.append(s.substr(p, stop - p))
		size += stop - p
		if size > MAX_STRING:
			code = "resource_limit"
			detail = "string_limit"
			return bad("string longer than %d" % MAX_STRING)
		p = stop + 1
		if stop == q:
			return {"t": kind, "v": "".join(out)}
		var esc: String = escape()
		if err != "":
			return {}
		out.append(esc)
	return {}

func escape() -> String:
	if p >= n:
		bad("unterminated escape")
		return ""
	var ch: String = s[p]
	p += 1
	if ch == "u" or ch == "U":
		var width: int = 4 if ch == "u" else 6
		var digits_text: String = s.substr(p, width)
		if digits_text.length() != width or not digits_text.is_valid_hex_number(false):
			bad("bad unicode escape")
			return ""
		p += width
		var cp: int = digits_text.hex_to_int()
		if cp == 0 or cp > 0x10FFFF or (cp >= 0xD800 and cp <= 0xDFFF):
			bad("unicode escape is not a usable scalar value")
			return ""
		return String.chr(cp)
	if ch == "0":
		bad("NUL escape is not supported")
		return ""
	return ESCAPES.get(ch, ch)

func _is_word(c: int) -> bool:
	return (c >= 48 and c <= 57) or (c >= 65 and c <= 90) or (c >= 97 and c <= 122) or c == 95
