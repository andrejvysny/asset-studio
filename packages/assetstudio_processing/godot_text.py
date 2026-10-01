"""Bounded static parser for Godot 4 text resources (.tscn/.tres, format=3).

Pure data: values become plain Python structures, nothing is evaluated, instantiated or resolved.
Grammar subset: contracts/godot-integration/v1/static-source-package.md §3.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 64
MAX_TOKENS = 2_000_000
MAX_STRING = 1024 * 1024
MAX_SECTIONS = 100_000

_WS = re.compile(r"(?:[ \t\r\n\x0b\x0c]|;[^\n]*)*")
_NUM = re.compile(r"[+-]?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_KEY = re.compile(r"[A-Za-z_][^\s=;\[\]{}()\",]*")
_PLAIN = re.compile(r'[^"\\]*')
_TYPE_ARG = re.compile(r"\[[A-Za-z0-9_]+\]")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "a": "\a", "v": "\v", "0": "\0"}
_SPECIAL_FLOATS = frozenset({"inf", "nan", "inf_neg"})
_BINARY_MAGIC = (b"RSRC", b"GDSC", b"GDRC", b"RSCC")
_ROOT_KINDS = ("gd_scene", "gd_resource")


class GodotTextError(ValueError):
    """`code`: unsafe_package (detail parse_error|binary_resource|unsupported_format|not_utf8) or resource_limit."""

    def __init__(self, code: str, detail: str, message: str, line: int | None = None) -> None:
        super().__init__(f"{message} (line {line})" if line else message)
        self.code, self.detail, self.line = code, detail, line


@dataclass(frozen=True)
class Num:
    text: str


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple[Any, ...]


@dataclass(frozen=True)
class Ref:
    kind: str  # ExtResource | SubResource
    id: str


@dataclass
class Section:
    kind: str
    attrs: dict[str, Any]
    props: list[tuple[str, Any]]
    line: int


@dataclass
class GodotTextDoc:
    kind: str  # gd_scene | gd_resource
    header: dict[str, Any]
    sections: list[Section] = field(default_factory=list)

    def ext_resources(self) -> list[Section]:
        return [s for s in self.sections if s.kind == "ext_resource"]

    def sub_resources(self) -> list[Section]:
        return [s for s in self.sections if s.kind == "sub_resource"]

    def nodes(self) -> list[Section]:
        return [s for s in self.sections if s.kind == "node"]

    def refs(self) -> list[Ref]:
        out = [r for v in self.header.values() for r in iter_refs(v)]
        for s in self.sections:
            out.extend(section_refs(s))
        return out


def walk(value: Any) -> Iterator[Any]:
    """Every node of a value tree (iterative; depth is bounded by the parser anyway)."""
    stack = [value]
    while stack:
        v = stack.pop()
        yield v
        if isinstance(v, list):
            stack.extend(v)
        elif isinstance(v, dict):
            stack.extend(v.keys())
            stack.extend(v.values())
        elif isinstance(v, Call):
            stack.extend(v.args)


def iter_refs(value: Any) -> Iterator[Ref]:
    return (v for v in walk(value) if isinstance(v, Ref))


def section_refs(sec: Section) -> list[Ref]:
    out = [r for v in sec.attrs.values() for r in iter_refs(v)]
    for _, v in sec.props:
        out.extend(iter_refs(v))
    return out


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.tokens = 0
        self._line, self._line_pos = 1, 0

    # -- helpers
    def line(self, pos: int | None = None) -> int:
        pos = self.pos if pos is None else pos
        if pos < self._line_pos:
            self._line, self._line_pos = 1, 0
        self._line += self.text.count("\n", self._line_pos, pos)
        self._line_pos = pos
        return self._line

    def bad(self, message: str) -> GodotTextError:
        return GodotTextError("unsafe_package", "parse_error", message, self.line())

    def tick(self) -> None:
        self.tokens += 1
        if self.tokens > MAX_TOKENS:
            raise GodotTextError("resource_limit", "token_limit", f"more than {MAX_TOKENS} tokens", self.line())

    def ws(self) -> None:
        self.pos = _WS.match(self.text, self.pos).end()  # type: ignore[union-attr]  # always matches

    def peek(self) -> str:
        self.ws()
        return self.text[self.pos:self.pos + 1]

    def expect(self, ch: str) -> None:
        if self.peek() != ch:
            raise self.bad(f"expected {ch!r}")
        self.pos += 1
        self.tick()

    def end_of_line(self) -> None:
        end = self.text.find("\n", self.pos)
        rest = self.text[self.pos:] if end < 0 else self.text[self.pos:end]
        if rest.split(";", 1)[0].strip():
            raise self.bad("unexpected text after value")

    # -- document
    def parse(self) -> GodotTextDoc:
        sections: list[Section] = []
        while self.peek():
            if self.peek() != "[":
                raise self.bad("expected a section header")
            if len(sections) >= MAX_SECTIONS:
                raise GodotTextError("resource_limit", "section_limit", f"more than {MAX_SECTIONS} sections",
                                     self.line())
            sections.append(self.section())
        if not sections or sections[0].kind not in _ROOT_KINDS:
            raise GodotTextError("unsafe_package", "parse_error", "file must start with [gd_scene] or [gd_resource]", 1)
        root = sections[0]
        if root.props:
            raise GodotTextError("unsafe_package", "parse_error", "properties after the file header", root.line)
        if root.attrs.get("format") != Num("3"):
            raise GodotTextError("unsafe_package", "unsupported_format", "only text format 3 is supported", root.line)
        return GodotTextDoc(root.kind, root.attrs, sections[1:])

    def section(self) -> Section:
        sec = self.header()
        while self.peek() not in ("", "["):
            self.ws()
            m = _KEY.match(self.text, self.pos)
            if not m:
                raise self.bad("expected a property name")
            self.pos = m.end()
            self.tick()
            self.expect("=")
            value = self.value(0)
            self.end_of_line()
            sec.props.append((m.group(), value))
        return sec

    def header(self) -> Section:
        self.ws()
        line = self.line()
        self.expect("[")
        self.ws()
        kind = _IDENT.match(self.text, self.pos)
        if not kind:
            raise self.bad("expected a section kind")
        self.pos = kind.end()
        self.tick()
        attrs: dict[str, Any] = {}
        while self.peek() != "]":
            name = _IDENT.match(self.text, self.pos)
            if not name or name.group() in attrs:
                raise self.bad("expected a unique attribute name")
            self.pos = name.end()
            self.tick()
            self.expect("=")
            attrs[name.group()] = self.value(0)
        self.pos += 1
        self.tick()
        self.end_of_line()
        return Section(kind.group(), attrs, [], line)

    # -- values
    def value(self, depth: int) -> Any:
        c = self.peek()
        self.tick()
        if c == '"':
            return self.string()
        if c in ("&", "^") and self.text.startswith('"', self.pos + 1):
            self.pos += 1
            return Call("StringName" if c == "&" else "NodePath", (self.string(),))
        if c == "[":
            return self.array(depth + 1)
        if c == "{":
            return self.dict(depth + 1)
        if c and c in "+-.0123456789":
            return self.number()
        ident = _IDENT.match(self.text, self.pos)
        if not ident:
            raise self.bad("unexpected character" if c else "unexpected end of input")
        return self.word(ident, depth)

    def number(self) -> Num:
        m = _NUM.match(self.text, self.pos)
        if m and not _IDENT.match(self.text, m.end()):
            self.pos = m.end()
            return Num(m.group())
        ident = _IDENT.match(self.text, self.pos + 1)
        if self.text[self.pos] == "-" and ident and ident.group() in _SPECIAL_FLOATS:
            self.pos = ident.end()
            return Num("-" + ident.group())
        raise self.bad("malformed number")

    def word(self, ident: re.Match[str], depth: int) -> Any:
        name = ident.group()
        self.pos = ident.end()
        if name in _SPECIAL_FLOATS:
            return Num(name)
        if name in ("true", "false"):
            return name == "true"
        if name == "null":
            return None
        if name in ("Array", "Dictionary"):
            typed = _TYPE_ARG.match(self.text, self.pos)
            if typed:
                self.pos = typed.end()
                name += typed.group()
        mark = self.pos
        if self.peek() != "(":
            self.pos = mark
            raise self.bad(f"unexpected identifier {name!r}")
        return self.call(name, depth + 1)

    def call(self, name: str, depth: int) -> Any:
        self.enter(depth)
        self.pos += 1  # "("
        args: list[Any] = []
        while self.peek() != ")":
            if args:
                self.expect(",")
            args.append(self.value(depth))
        self.pos += 1
        self.tick()
        if name in ("ExtResource", "SubResource"):
            if len(args) != 1 or not isinstance(args[0], str | Num):
                raise self.bad(f"{name} takes one id")
            arg = args[0]
            return Ref(name, arg if isinstance(arg, str) else arg.text)
        return Call(name, tuple(args))

    def array(self, depth: int) -> list[Any]:
        self.enter(depth)
        self.pos += 1
        items: list[Any] = []
        while self.peek() != "]":
            if items:
                self.expect(",")
            items.append(self.value(depth))
        self.pos += 1
        self.tick()
        return items

    def dict(self, depth: int) -> dict[Any, Any]:
        self.enter(depth)
        self.pos += 1
        out: dict[Any, Any] = {}
        while self.peek() != "}":
            if out:
                self.expect(",")
            key = self.value(depth)
            try:
                hash(key)
            except TypeError:
                raise self.bad("unhashable dictionary key") from None
            self.expect(":")
            out[key] = self.value(depth)
        self.pos += 1
        self.tick()
        return out

    def enter(self, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise GodotTextError("resource_limit", "depth_limit", f"nesting deeper than {MAX_DEPTH}", self.line())

    def string(self) -> str:
        start = self.pos
        self.pos += 1
        parts: list[str] = []
        size = 0
        while True:
            m = _PLAIN.match(self.text, self.pos)
            chunk = m.group()  # type: ignore[union-attr]  # pattern can match empty
            parts.append(chunk)
            self.pos = m.end()  # type: ignore[union-attr]
            size += len(chunk)
            if size > MAX_STRING:
                raise GodotTextError("resource_limit", "string_limit", f"string longer than {MAX_STRING}",
                                     self.line(start))
            ch = self.text[self.pos:self.pos + 1]
            if not ch:
                raise GodotTextError("unsafe_package", "parse_error", "unterminated string", self.line(start))
            self.pos += 1
            if ch == '"':
                return "".join(parts)
            esc = self.escape()
            parts.append(esc)
            size += len(esc)

    def escape(self) -> str:
        ch = self.text[self.pos:self.pos + 1]
        self.pos += 1
        if ch in ("u", "U"):
            width = 4 if ch == "u" else 6
            digits = self.text[self.pos:self.pos + width]
            if len(digits) != width or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                raise self.bad("bad unicode escape")
            self.pos += width
            code = int(digits, 16)
            if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
                raise self.bad("unicode escape is not a scalar value")
            return chr(code)
        if not ch:
            raise self.bad("unterminated escape")
        return _ESCAPES.get(ch, ch)


def parse_godot_text(data: bytes) -> GodotTextDoc:
    """Parse a text resource; raises GodotTextError (unsafe_package | resource_limit)."""
    if len(data) > MAX_INPUT_BYTES:
        raise GodotTextError("resource_limit", "input_limit", f"text resource larger than {MAX_INPUT_BYTES} bytes")
    if data.startswith(_BINARY_MAGIC):
        raise GodotTextError("unsafe_package", "binary_resource", "binary resource is not Godot text format", 1)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise GodotTextError("unsafe_package", "not_utf8", f"not valid UTF-8: {e.reason}", 1) from e
    text = text.removeprefix("﻿").replace("\r\n", "\n")
    return _Parser(text).parse()
