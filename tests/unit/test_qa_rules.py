from pathlib import Path

import yaml
from jobcore.qa_rules import evaluate

RULES = yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "qa" / "rules.yaml").read_text())
GOOD_MASK = {"fill_ratio": 0.3, "touches_border": False, "components": 1, "secondary_ratio": 0.0}
ALL_OK = {"checks": {k: True for k in RULES["vlm_checks"]}, "reasons": [], "summary": "fine"}


def test_all_ok_recommended() -> None:
    r = evaluate("00.png", ALL_OK, GOOD_MASK, RULES)
    assert r["recommended"] and r["status"] == "recommended" and not r["reasons"]


def test_major_fail_not_recommended() -> None:
    vlm = {**ALL_OK, "checks": {**ALL_OK["checks"], "single_object": False}, "reasons": ["two barrels"]}
    r = evaluate("00.png", vlm, GOOD_MASK, RULES)
    assert not r["recommended"] and r["failed_major"] == ["single_object"] and "two barrels" in r["reasons"]


def test_minor_fail_limit() -> None:
    one = {**ALL_OK, "checks": {**ALL_OK["checks"], "no_shadow": False}}
    assert evaluate("00.png", one, GOOD_MASK, RULES)["recommended"]
    two = {**ALL_OK, "checks": {**ALL_OK["checks"], "no_shadow": False, "no_floor": False}}
    assert not evaluate("00.png", two, GOOD_MASK, RULES)["recommended"]


def test_mask_cropped_and_multi_blob() -> None:
    mask = {**GOOD_MASK, "touches_border": True, "components": 3, "secondary_ratio": 0.4}
    r = evaluate("00.png", ALL_OK, mask, RULES)
    assert set(r["failed_major"]) == {"mask_not_cropped", "mask_single_blob"}


def test_no_qa_is_unverified_not_green() -> None:
    r = evaluate("00.png", None, None, RULES, vlm_error="timeout", mask_error="down")
    assert r["status"] == "unverified" and not r["recommended"]
    assert r["coverage"]["ran"] == 0 and len(r["coverage"]["missing"]) == r["coverage"]["total"]
    assert any("timeout" in w for w in r["warnings"]) and r["services"]["vlm"] == "timeout"


def test_empty_vlm_checks_is_unverified() -> None:
    r = evaluate("00.png", {"checks": {}, "reasons": []}, GOOD_MASK, RULES)
    assert r["status"] == "unverified"
    assert r["coverage"]["ran"] == 3 and "single_object" in r["coverage"]["missing"]


def test_mask_only_is_unverified() -> None:
    r = evaluate("00.png", None, GOOD_MASK, RULES)
    assert r["status"] == "unverified" and not r["recommended"]


def test_failure_beats_missing_coverage() -> None:
    r = evaluate("00.png", None, {**GOOD_MASK, "touches_border": True}, RULES)
    assert r["status"] == "not_recommended"


def test_reasons_derived_when_model_gives_none() -> None:
    vlm = {**ALL_OK, "checks": {**ALL_OK["checks"], "no_text": False}, "reasons": []}
    r = evaluate("00.png", vlm, GOOD_MASK, RULES)
    assert r["status"] == "not_recommended" and any("text" in x.lower() for x in r["reasons"])


def test_full_coverage_recommended() -> None:
    r = evaluate("00.png", ALL_OK, GOOD_MASK, RULES)
    assert r["coverage"] == {"ran": r["coverage"]["total"], "total": r["coverage"]["total"], "missing": []}
