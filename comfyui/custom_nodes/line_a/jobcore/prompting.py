"""Prompt versions: user edits the description; the model-sheet template is always appended exactly once."""
from __future__ import annotations

import re


def split_template(text: str, template: str) -> str:
    """Return the descriptive part of an enhanced prompt (template removed, wherever it appears)."""
    pattern = re.compile(re.escape(template.strip().rstrip(".")), re.IGNORECASE)
    desc = pattern.sub("", text)
    desc = re.sub(r"\s*,\s*(,\s*)+", ", ", desc)  # collapse separators left behind
    return desc.strip().strip(",").strip()


def compose_effective(description: str, template: str) -> str:
    desc = split_template(description, template).rstrip(" ,.")
    if not desc:
        raise ValueError("prompt description is empty")
    return f"{desc}, {template.strip()}"
