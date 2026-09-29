"""Structured shot-list import (CSV / YAML / Markdown pipe table). Parse + validate only; no LLM repair."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any

from .kinds import Kind
from .safeyaml import ParseError, load_yaml

FIELDS = ("id", "name", "category", "kind", "brief", "priority", "notes", "target_asset_id")
ALIASES = {"type": "kind", "asset_type": "kind", "prio": "priority", "description": "brief", "external_id": "id",
           "category_id": "category", "title": "name"}
MAX_ROWS = 5000
MAX_BYTES = 2 * 1024 * 1024


@dataclass
class ParsedRow:
    line: int
    values: dict[str, str]
    errors: list[str] = field(default_factory=list)


@dataclass
class ParseResult:
    format: str
    columns: list[str]
    mapping: dict[str, str | None]
    rows: list[ParsedRow]
    errors: list[str] = field(default_factory=list)


def _map_columns(columns: list[str]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for col in columns:
        key = col.strip().lower().replace(" ", "_")
        key = ALIASES.get(key, key)
        out[col] = key if key in FIELDS else None
    return out


def _rows_from_table(header: list[str], body: list[tuple[int, list[str]]], fmt: str) -> ParseResult:
    mapping = _map_columns(header)
    errors = [] if "name" in mapping.values() else ["no 'name' column"]
    rows: list[ParsedRow] = []
    for line, cells in body:
        row = ParsedRow(line=line, values={})
        if len(cells) != len(header):
            row.errors.append(f"expected {len(header)} cells, got {len(cells)}")
        for col, cell in zip(header, cells, strict=False):
            target = mapping[col]
            if target:
                row.values[target] = cell.strip()
        rows.append(row)
    return ParseResult(fmt, header, mapping, rows, errors)


def parse_csv(text: str) -> ParseResult:
    reader = csv.reader(io.StringIO(text), strict=True)
    try:
        table = [(reader.line_num, r) for r in reader if any(c.strip() for c in r)]
    except csv.Error as e:
        return ParseResult("csv", [], {}, [], [f"line {reader.line_num}: {e}"])
    if not table:
        return ParseResult("csv", [], {}, [], ["empty file"])
    return _rows_from_table(table[0][1], table[1:], "csv")


def parse_markdown(text: str) -> ParseResult:
    lines = [(i + 1, ln.strip()) for i, ln in enumerate(text.splitlines())]
    table = [(n, ln) for n, ln in lines if ln.startswith("|")]
    if len(table) < 2:
        return ParseResult("markdown", [], {}, [], [
            "no pipe table found: use a Markdown table with a header row, e.g. | name | category | kind | brief |"])

    def cells(ln: str) -> list[str]:
        return [c.strip() for c in ln.strip("|").split("|")]

    header = cells(table[0][1])
    sep = cells(table[1][1])
    if not all(set(c) <= set(":-") and c for c in sep):
        msg = f"line {table[1][0]}: second table row must be a ---|--- separator"
        return ParseResult("markdown", header, {}, [], [msg])
    return _rows_from_table(header, [(n, cells(ln)) for n, ln in table[2:]], "markdown")


def parse_yaml(text: str) -> ParseResult:
    try:
        data: Any = load_yaml(text)
    except ParseError as e:
        return ParseResult("yaml", [], {}, [], [f"line {e.line}: {e}" if e.line else str(e)])
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return ParseResult("yaml", [], {}, [], ["expected a list of rows, or a mapping with an 'items' list"])
    columns = sorted({str(k) for it in items if isinstance(it, dict) for k in it})
    mapping = _map_columns(columns)
    rows: list[ParsedRow] = []
    for idx, it in enumerate(items):
        row = ParsedRow(line=idx + 1, values={})
        if not isinstance(it, dict):
            row.errors.append("row is not a mapping")
        else:
            for k, v in it.items():
                target = mapping.get(str(k))
                if target and v is not None:
                    if not isinstance(v, str | int | float):
                        row.errors.append(f"{k}: expected text")
                    else:
                        row.values[target] = str(v).strip()
        rows.append(row)
    return ParseResult("yaml", columns, mapping, rows)


def parse(filename: str, content: bytes) -> ParseResult:
    if len(content) > MAX_BYTES:
        return ParseResult("unknown", [], {}, [], [f"file larger than {MAX_BYTES} bytes"])
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ParseResult("unknown", [], {}, [], ["file is not UTF-8"])
    lower = filename.lower()
    if lower.endswith(".csv"):
        res = parse_csv(text)
    elif lower.endswith((".yaml", ".yml")):
        res = parse_yaml(text)
    elif lower.endswith((".md", ".markdown")):
        res = parse_markdown(text)
    else:
        return ParseResult("unknown", [], {}, [], ["unsupported file type: use .csv, .yaml/.yml or .md"])
    if len(res.rows) > MAX_ROWS:
        res.errors.append(f"more than {MAX_ROWS} rows")
    return res


def validate_row(row: ParsedRow, category_ids: set[str], category_kinds: dict[str, str | None]) -> None:
    v = row.values
    if not v.get("name"):
        row.errors.append("name is required")
    cat = v.get("category") or None
    if cat is not None and cat not in category_ids:
        row.errors.append(f"unknown category {cat!r}")
    kind = v.get("kind") or None
    if kind is not None:
        norm = kind.strip().lower().replace(" ", "_")
        if norm not in Kind.__members__:
            row.errors.append(f"unknown kind {kind!r}; use one of {', '.join(k.value for k in Kind)}")
        else:
            v["kind"] = norm
    elif cat is None or not category_kinds.get(cat):
        row.errors.append("kind is required (category has no default kind)")
    prio = (v.get("priority") or "med").lower()
    prio = {"medium": "med", "normal": "med"}.get(prio, prio)
    if prio not in ("low", "med", "high"):
        row.errors.append(f"priority must be low/med/high, got {v.get('priority')!r}")
    v["priority"] = prio
