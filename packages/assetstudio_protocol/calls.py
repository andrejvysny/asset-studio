"""Per-operation call contract: the `Offer.params` schema of every operation and the result-file encoding.

Images and other binary inputs never travel in params; they are `Offer.inputs` (role/label select their use).
Results are a set of named files: aux results become `result.json` plus one binary file per top-level bytes value.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from assetstudio_core.canonical import canonical_json

from .base import Msg, Sha256

__all__ = ["LoraParam", "T2IParams", "EditParams", "AuxEnhanceParams", "AuxCompareParams", "AuxQaParams",
           "AuxCutoutParams", "AuxAnalyzeParams", "AuxSuggestParams", "Worker3dParams", "PARAMS_BY_OPERATION",
           "parse_params", "encode_result", "decode_result"]

FILE_KEY = "$file"


class LoraParam(Msg):
    file: str
    strength: float


class T2IParams(Msg):
    prompt_id: str
    positive: str
    negative: str
    seed: int
    width: int
    height: int
    steps: int
    cfg: float
    filename_prefix: str
    style_lora: LoraParam | None = None
    speed_lora: LoraParam | None = None


class EditParams(Msg):
    """The prepared source image is the single input with role "image"."""
    prompt_id: str
    source_sha256: Sha256
    prepared_input_sha256: Sha256
    positive: str
    negative: str
    seed: int
    steps: int
    cfg: float
    filename_prefix: str
    output_profile_id: str = "edit.default"
    recipe_version: int = 1


class AuxEnhanceParams(Msg):
    """Inputs (optional): images; each (role, label) becomes the aux (bytes, role, label) tuple."""
    execution_id: str | None = None
    brief: str
    kind: str
    constraints: str
    style_guide: str
    preset: str = "conservative"
    mode: str = "t2i"
    preserve: str = ""
    change: str = ""


class AuxCompareParams(Msg):
    """Inputs: images; (role, label) -> (bytes, role, label), e.g. ("candidate", "A")."""
    execution_id: str | None = None
    questions: list[tuple[str, str]]
    context: str


class AuxQaParams(Msg):
    """Single input with role "image"."""
    execution_id: str | None = None
    questions: list[tuple[str, str]]
    context: str


class AuxCutoutParams(Msg):
    """Single input with role "image"."""
    execution_id: str | None = None


class AuxAnalyzeParams(Msg):
    """Inputs: images; the input role is the view name."""
    execution_id: str | None = None
    kind: str
    user_facts: str = ""


class AuxSuggestParams(Msg):
    """Inputs: images; the input role is the view name."""
    execution_id: str | None = None
    request: str
    count: int
    intent: str
    preserve: str
    kind: str
    observations: list[str] | None = None


class Worker3dParams(Msg):
    """Single input with role "body" (image for generate, raw intermediate for export)."""
    execution_id: str
    op: Literal["generate", "export"]
    params: dict[str, Any]


PARAMS_BY_OPERATION: dict[str, type[Msg]] = {
    "image.t2i": T2IParams, "image.edit": EditParams, "aux.enhance": AuxEnhanceParams,
    "aux.compare": AuxCompareParams, "aux.qa": AuxQaParams, "aux.cutout": AuxCutoutParams,
    "aux.analyze_source": AuxAnalyzeParams, "aux.suggest_variants": AuxSuggestParams,
    "worker3d.generate": Worker3dParams, "worker3d.export": Worker3dParams,
}


def parse_params(operation: str, params: dict[str, Any]) -> Msg:
    """Raises ValueError (pydantic ValidationError included) for an unknown operation or a malformed params dict."""
    model = PARAMS_BY_OPERATION.get(operation)
    if model is None:
        raise ValueError(f"unknown operation {operation!r}")
    return model.model_validate(params)


def _contains_bytes(value: Any) -> bool:
    if isinstance(value, bytes):
        return True
    if isinstance(value, dict):
        return any(_contains_bytes(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_bytes(v) for v in value)
    return False


def _is_marker(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {FILE_KEY}


def encode_result(result: dict[str, Any]) -> tuple[bytes, dict[str, bytes]]:
    """Returns (canonical JSON for result.json, {name: bytes}). Each top-level bytes value moves to its own file and
    is replaced by {"$file": name}. Nested bytes are refused: aux and worker3d results only carry top-level
    binaries (masks), and a nested reference scheme would need path semantics nobody needs."""
    doc: dict[str, Any] = {}
    files: dict[str, bytes] = {}
    for key, value in result.items():
        if isinstance(value, bytes):
            doc[key], files[key] = {FILE_KEY: key}, value
            continue
        if _contains_bytes(value):
            raise TypeError(f"result key {key!r}: nested bytes are not supported")
        if _is_marker(value):
            raise ValueError(f"result key {key!r} collides with the file marker")
        doc[key] = value
    return canonical_json(doc), files


def decode_result(json_bytes: bytes, files: dict[str, bytes]) -> dict[str, Any]:
    doc = json.loads(json_bytes)
    if not isinstance(doc, dict):
        raise ValueError("result.json must be a JSON object")
    out: dict[str, Any] = {}
    for key, value in doc.items():
        if _is_marker(value):
            name = value[FILE_KEY]
            if name not in files:
                raise ValueError(f"result file {name!r} is missing")
            out[key] = files[name]
        else:
            out[key] = value
    return out
