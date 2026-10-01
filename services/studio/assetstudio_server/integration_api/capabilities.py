"""Contract documents served to clients: loaded once from `settings.contracts_dir`, cached."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ContractsMissing(RuntimeError):
    pass


@dataclass(frozen=True)
class Contracts:
    capabilities: dict[str, Any]
    error_codes: dict[str, Any]


_CACHE: dict[Path, Contracts] = {}


def _read(directory: Path, name: str) -> dict[str, Any]:
    path = directory / name
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, ValueError) as e:
        raise ContractsMissing(
            f"integration contract file {path} is missing or invalid; set STUDIO_CONTRACTS_DIR") from e
    if not isinstance(data, dict):
        raise ContractsMissing(f"integration contract file {path} must be a JSON object")
    return data


def load_contracts(directory: Path) -> Contracts:
    if directory not in _CACHE:
        _CACHE[directory] = Contracts(_read(directory, "capabilities.json"), _read(directory, "error-codes.json"))
    return _CACHE[directory]
