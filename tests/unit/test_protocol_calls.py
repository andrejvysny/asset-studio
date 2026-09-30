"""Per-operation params schema and the result-file encoding shared by Studio and runners."""
from __future__ import annotations

import json

import pytest
from assetstudio_protocol import calls
from assetstudio_protocol.execution import OPERATION_VERSIONS

SHA = "a" * 64

VALID: dict[str, dict] = {
    "image.t2i": {"prompt_id": "p", "positive": "a", "negative": "b", "seed": 1, "width": 64, "height": 64,
                  "steps": 4, "cfg": 1.0, "filename_prefix": "x", "speed_lora": {"file": "l.safetensors",
                                                                                 "strength": 1.0}},
    "image.edit": {"prompt_id": "p", "source_sha256": SHA, "prepared_input_sha256": SHA, "positive": "a",
                   "negative": "b", "seed": 1, "steps": 4, "cfg": 1.0, "filename_prefix": "x"},
    "aux.enhance": {"execution_id": None, "brief": "b", "kind": "k", "constraints": "c", "style_guide": "s"},
    "aux.compare": {"execution_id": "e", "questions": [["q1", "is it?"]], "context": "c"},
    "aux.qa": {"execution_id": "e", "questions": [["q1", "is it?"]], "context": "c"},
    "aux.cutout": {},
    "aux.analyze_source": {"execution_id": "e", "kind": "k"},
    "aux.suggest_variants": {"execution_id": "e", "request": "r", "count": 3, "intent": "i", "preserve": "p",
                             "kind": "k", "observations": ["o"]},
    "worker3d.generate": {"execution_id": "e", "op": "generate", "params": {"seed": 1}},
    "worker3d.export": {"execution_id": "e", "op": "export", "params": {"exporter": "clean"}},
}


def test_every_operation_has_a_params_model() -> None:
    assert set(calls.PARAMS_BY_OPERATION) == set(OPERATION_VERSIONS) == set(VALID)


@pytest.mark.parametrize("op", sorted(VALID))
def test_parse_params_round_trips_json(op: str) -> None:
    parsed = calls.parse_params(op, VALID[op])
    assert isinstance(parsed, calls.PARAMS_BY_OPERATION[op])
    wire = json.loads(parsed.model_dump_json())  # what an Offer carries
    assert calls.parse_params(op, wire) == parsed


def test_parse_params_defaults_and_tuples() -> None:
    enh = calls.parse_params("aux.enhance", VALID["aux.enhance"])
    assert (enh.preset, enh.mode, enh.preserve, enh.change) == ("conservative", "t2i", "", "")  # type: ignore[attr-defined]
    cmp_ = calls.parse_params("aux.compare", VALID["aux.compare"])
    assert cmp_.questions == [("q1", "is it?")]  # type: ignore[attr-defined]
    edit = calls.parse_params("image.edit", VALID["image.edit"])
    assert (edit.output_profile_id, edit.recipe_version) == ("edit.default", 1)  # type: ignore[attr-defined]


def test_parse_params_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="unknown operation"):
        calls.parse_params("image.nope", {})
    with pytest.raises(ValueError):
        calls.parse_params("image.t2i", {**VALID["image.t2i"], "extra": 1})
    with pytest.raises(ValueError):
        calls.parse_params("image.edit", {**VALID["image.edit"], "source_sha256": "zz"})
    with pytest.raises(ValueError):
        calls.parse_params("worker3d.generate", {"execution_id": "e", "op": "bake", "params": {}})
    with pytest.raises(ValueError):
        calls.parse_params("aux.compare", {"execution_id": "e", "context": "c"})


def test_result_encoding_round_trip() -> None:
    result = {"mask_png": b"\x89PNG\x00bytes", "meta": {"model": "m", "seconds": 0.5}, "rows": [1, 2],
              "note": None}
    doc, files = calls.encode_result(result)
    assert files == {"mask_png": b"\x89PNG\x00bytes"}
    assert json.loads(doc)["mask_png"] == {"$file": "mask_png"}
    assert doc == calls.encode_result(dict(reversed(list(result.items()))))[0]  # canonical: key order independent
    assert calls.decode_result(doc, files) == result
    no_bin, none = calls.encode_result({"a": 1})
    assert none == {} and calls.decode_result(no_bin, {}) == {"a": 1}


def test_result_encoding_errors() -> None:
    with pytest.raises(TypeError, match="nested bytes"):
        calls.encode_result({"meta": {"raw": b"x"}})
    with pytest.raises(TypeError, match="nested bytes"):
        calls.encode_result({"list": [b"x"]})
    with pytest.raises(ValueError, match="marker"):
        calls.encode_result({"sneaky": {"$file": "x"}})
    doc, _ = calls.encode_result({"m": b"x"})
    with pytest.raises(ValueError, match="missing"):
        calls.decode_result(doc, {})
    with pytest.raises(ValueError):
        calls.decode_result(b"[1]", {})
