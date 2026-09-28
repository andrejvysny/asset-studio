"""Paths + app.yaml access. Container paths by default; override via env for tests."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(os.environ.get("LINE_A_CONFIG_DIR", "/config"))
OUTPUT_ROOT = Path(os.environ.get("LINE_A_OUTPUT_ROOT", "/output"))


@lru_cache(maxsize=1)
def app_config() -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / "app.yaml").read_text())


def qa_rules() -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / "qa" / "rules.yaml").read_text())


def models_config() -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / "models.yaml").read_text())["models"]


def model_pins() -> dict[str, dict[str, str]]:
    return {k: {"repo": v["repo"], "revision": v["revision"]} for k, v in models_config().items()}


def read_prompt_file(name: str) -> str:
    return (CONFIG_DIR / "prompts" / name).read_text().strip()
