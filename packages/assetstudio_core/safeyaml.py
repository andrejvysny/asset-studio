"""Bounded YAML/JSON parsing for untrusted configuration and imports."""
from __future__ import annotations

from typing import Any

import yaml

MAX_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 32


class ParseError(ValueError):
    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.line = line


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    seen: set = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise ParseError(f"duplicate key {key!r}", key_node.start_mark.line + 1)
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _depth(obj: Any, level: int = 0) -> int:
    if level > MAX_DEPTH:
        return level
    if isinstance(obj, dict):
        return max((_depth(v, level + 1) for v in obj.values()), default=level)
    if isinstance(obj, list):
        return max((_depth(v, level + 1) for v in obj), default=level)
    return level


def load_yaml(text: str | bytes, max_bytes: int = MAX_BYTES) -> Any:
    raw = text.encode() if isinstance(text, str) else text
    if len(raw) > max_bytes:
        raise ParseError(f"document larger than {max_bytes} bytes")
    try:
        data = yaml.load(raw.decode("utf-8"), Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass
    except ParseError:
        raise
    except UnicodeDecodeError as e:
        raise ParseError("document is not UTF-8") from e
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        raise ParseError(f"invalid YAML: {getattr(e, 'problem', e)}", mark.line + 1 if mark else None) from e
    if _depth(data) > MAX_DEPTH:
        raise ParseError(f"nesting deeper than {MAX_DEPTH}")
    return data


def dump_yaml(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
