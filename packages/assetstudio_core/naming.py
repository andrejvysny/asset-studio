"""Allowlisted naming templates for friendly asset ids and export paths. No expressions, no format specs."""
from __future__ import annotations

import re
import unicodedata

NAME_VARS = {"name", "category", "sub", "variant", "v", "kind"}
# {v} is the variant letter (design compatibility); {version} is the published version number.
PATH_VARS = NAME_VARS | {"asset_id", "version", "filename", "ext"}
_VAR_RE = re.compile(r"\{([^{}]*)\}")
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def slug(text: str, max_len: int = 48) -> str:
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "_", norm.lower()).strip("_")[:max_len].strip("_")
    return s or "asset"


def validate_template(template: str, path: bool = False) -> str | None:
    if not template or len(template) > 200:
        return "template must be 1–200 characters"
    allowed = PATH_VARS if path else NAME_VARS
    for var in _VAR_RE.findall(template):
        if var not in allowed:
            return f"unknown variable {{{var}}}; allowed: {', '.join('{' + v + '}' for v in sorted(allowed))}"
    rest = _VAR_RE.sub("", template)
    if "{" in rest or "}" in rest:
        return "unbalanced braces"
    if not path and "/" in rest:
        return "a name template must not contain '/'"
    return None


def render(template: str, values: dict[str, str]) -> str:
    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key == "v":
            key = "variant"
        return values.get(key, "")

    return _VAR_RE.sub(sub, template)


def render_name(template: str, name: str, category_slug: str, sub: str, kind: str, variant: str) -> str:
    raw = render(template, {"name": slug(name), "category": category_slug, "sub": sub, "variant": variant,
                            "kind": kind})
    return re.sub(r"_+", "_", raw).strip("_") or slug(name)


def variant_letters() -> list[str]:
    letters = [chr(c) for c in range(ord("a"), ord("z") + 1)]
    return letters + [a + b for a in letters for b in letters]


def safe_path_component(part: str) -> str | None:
    """Returns an error message, or None if the component is safe on Linux/macOS/Windows."""
    if part in ("", ".", ".."):
        return f"invalid path component {part!r}"
    if any(ch in part for ch in '<>:"\\|?*\x00') or any(ord(ch) < 32 for ch in part):
        return f"invalid character in {part!r}"
    if part.split(".")[0].lower() in _WINDOWS_RESERVED or part.endswith((" ", ".")):
        return f"reserved name {part!r}"
    return None


def collision_key(path: str) -> str:
    """Two export paths that collide on case-insensitive or differently-normalized filesystems share a key."""
    return unicodedata.normalize("NFC", path).casefold()
