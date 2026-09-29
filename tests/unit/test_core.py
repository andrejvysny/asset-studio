"""Core domain: inheritance (G03-G06), QA policy (Q01-Q03, Q06), seeds, naming, shot-list parsing (I03)."""
from __future__ import annotations

import numpy as np
import pytest
from assetstudio_core import config, inheritance, qa, seeds, shotlist_io
from assetstudio_core.defaults import new_project_config
from assetstudio_core.naming import collision_key, render_name, safe_path_component, validate_template
from assetstudio_core.safeyaml import ParseError, load_yaml
from assetstudio_processing import metrics


def cfg_with(*cats: dict) -> config.StudioConfig:
    c = new_project_config("prj_0000000000000000", "T")
    c.categories = [config.Category.model_validate(x) for x in cats]
    return c


PROPS = {"id": "props", "slug": "props", "label": "Props",
         "defaults": {"kind": "model3d", "style_lora": {"model_id": "l1", "strength": 0.6},
                      "budget": {"triangles": {"min": 300, "max": 2500}}}}


def test_inheritance_sources_disable_and_zero_strength() -> None:
    c = cfg_with(PROPS,
                 {"id": "boxes", "parent_id": "props", "slug": "boxes", "label": "Boxes",
                  "defaults": {"budget": {"triangles": {"min": 300, "max": 1200}}, "style_lora": {"mode": "disabled"}}},
                 {"id": "tools", "parent_id": "props", "slug": "tools", "label": "Tools",
                  "defaults": {"style_lora": {"model_id": "l1", "strength": 0.0}}},
                 {"id": "misc", "parent_id": "props", "slug": "misc", "label": "Misc"})
    assert inheritance.validate_semantics(c) == []
    boxes = inheritance.resolve(c, "boxes")
    assert boxes["kind"].value == "model3d" and boxes["kind"].source == "category:props"
    assert boxes["budget"].value["triangles"]["max"] == 1200 and boxes["budget"].source == "category:boxes"
    assert boxes["style_lora"].value is None and boxes["style_lora"].mode == config.Mode.disabled
    assert inheritance.resolve(c, "tools")["style_lora"].value["strength"] == 0.0
    misc = inheritance.resolve(c, "misc")  # "reset": no local value -> inherits the parent's
    assert misc["style_lora"].value["strength"] == 0.6 and misc["style_lora"].source == "category:props"
    assert misc["recipe_id"].value == "model3d.default" and misc["recipe_id"].source == "recipe"
    assert misc["qa_ruleset"].value == "model3d"


@pytest.mark.parametrize("cats,needle", [
    ([{"id": "a", "parent_id": "b", "slug": "a", "label": "A"}, {"id": "b", "parent_id": "a", "slug": "b",
                                                                  "label": "B"}], "cycle"),
    ([{"id": "a", "parent_id": "zzz", "slug": "a", "label": "A"}], "unknown parent"),
    ([{"id": "a", "slug": "x", "label": "A"}, {"id": "b", "slug": "x", "label": "B"}], "duplicate sibling slug"),
    ([{"id": "a", "slug": "a", "label": "A", "defaults": {"qa_ruleset": "nope"}}], "unknown qa_ruleset"),
    ([{"id": "a", "slug": "a", "label": "A", "defaults": {"naming": "{name}_{evil}"}}], "unknown variable"),
])
def test_invalid_configs(cats: list, needle: str) -> None:
    errs = inheritance.validate_semantics(cfg_with(*cats))
    assert any(needle in e.message for e in errs), errs


def test_override_shapes() -> None:
    with pytest.raises(ValueError):
        config.Override[str].model_validate({"mode": "value"})
    with pytest.raises(ValueError):
        config.Override[str].model_validate({"mode": "disabled", "value": "x"})
    assert config.Override[str].model_validate(None).mode == config.Mode.inherit


def test_snapshot_is_stable_and_isolated_from_later_edits() -> None:
    c = cfg_with(PROPS)
    s1 = inheritance.build_snapshot(c, "props")
    assert s1 == inheritance.build_snapshot(c, "props")
    c.qa_rulesets["model3d"].rules[0].severity = "minor"
    s2 = inheritance.build_snapshot(c, "props")
    assert s1["sha256"] != s2["sha256"] and s1["qa_ruleset"]["rules"][0]["severity"] == "major"


def test_yaml_roundtrip_and_duplicates() -> None:
    from assetstudio_storage.project import ProjectStore

    c = cfg_with(PROPS)
    parsed, errs = config.parse_config(load_yaml(ProjectStore.config_text(c)))
    assert errs == [] and parsed == c
    with pytest.raises(ParseError, match="duplicate"):
        load_yaml("a: 1\na: 2\n")
    _, errs = config.parse_config({"project": {"id": "x", "name": "y"}, "surprise": 1})
    assert errs and errs[0].path == "surprise"


def rule(rid: str, sev: str = "major", source: str = "vlm", enabled: bool = True) -> qa.QaRule:
    if source == "vlm":
        return qa.QaRule(id=rid, source="vlm", question="q?", severity=sev, enabled=enabled)
    return qa.QaRule(id=rid, source="mask_metric", metric="mask_fill", severity=sev, enabled=enabled)


def res(r: qa.QaRule, result: str) -> qa.CheckResult:
    return qa.CheckResult(rule_id=r.id, source=r.source, severity=r.severity, result=result)


def test_policy_major_minor_unavailable_zero() -> None:
    a, b, c, d = rule("a"), rule("b", "minor"), rule("c", "minor"), rule("d", enabled=False)
    pol = qa.QaPolicy()
    assert qa.evaluate_policy([a, b], [res(a, "fail"), res(b, "pass")], pol)["status"] == "not_recommended"
    assert qa.evaluate_policy([a, b, c], [res(a, "pass"), res(b, "fail"), res(c, "fail")], pol)["status"] == \
        "not_recommended"
    one_minor = qa.evaluate_policy([a, b], [res(a, "pass"), res(b, "fail")], pol)
    assert one_minor["status"] == "recommended"
    unv = qa.evaluate_policy([a, b], [res(a, "pass"), res(b, "unavailable")], pol)
    assert unv["status"] == "unverified" and unv["coverage"] == {"completed": 1, "applicable": 2}
    zero = qa.evaluate_policy([d], [], pol)
    assert zero["status"] == "unverified" and zero["not_evaluated"] and zero["disabled"] == ["d"]
    na = qa.evaluate_policy([a], [res(a, "not_applicable")], pol)
    assert na["status"] == "unverified" and na["coverage"]["applicable"] == 0


def test_strict_vlm_parse() -> None:
    out = qa.parse_vlm_answers({"a": True, "b": "false", "c": 0}, ["a", "b", "c", "d"])
    assert out["a"] is True and all(isinstance(out[k], str) for k in "bcd")
    assert isinstance(qa.parse_vlm_answers("nope", ["a"])["a"], str)


def test_metric_rules_are_allowlisted() -> None:
    with pytest.raises(ValueError):
        qa.QaRule(id="x", source="image_metric", metric="os.system")
    with pytest.raises(ValueError):
        qa.QaRule(id="x", source="mask_metric", metric="mask_fill", params={"code": "1"})


def test_mask_metrics_and_palette_scope() -> None:
    mask = np.zeros((100, 100), np.uint8)
    mask[30:70, 30:70] = 255
    st = metrics.mask_stats(mask)
    assert st["fill_ratio"] == 0.16 and not st["touches_border"] and st["components"] == 1
    mask[0:3, 0:3] = 255
    assert metrics.mask_stats(mask)["touches_border"] and metrics.mask_stats(mask)["components"] == 2
    rgb = np.zeros((10, 10, 3), np.uint8)
    rgb[:5] = (0, 0, 255)
    cov = metrics.reserved_coverage(rgb, [("#0000ff", 8.0)], None)
    assert cov["#0000ff"] == 0.5
    from assetstudio_server.coordinator.tasks_qa import reserved_colours
    style = {"palette": [{"hex": "#0000FF", "reserved": True, "allowed_kinds": ["icon"], "allowed_categories": [],
                          "tolerance_delta_e": 8.0}]}
    base = {"style": style, "category_chain": []}
    assert reserved_colours({**base, "recipe": {"kind": "icon"}}) == []
    assert reserved_colours({**base, "recipe": {"kind": "sprite"}}) == [("#0000ff", 8.0)]


def test_seeds_deterministic_and_distinct() -> None:
    s = [seeds.derive_seed(42, "itm_a", "1", str(i)) for i in range(4)]
    assert s == [seeds.derive_seed(42, "itm_a", "1", str(i)) for i in range(4)] and len(set(s)) == 4
    assert seeds.derive_seed(43, "itm_a", "1", "0") != s[0]
    assert all(0 <= x < 2**53 for x in s)


def test_naming() -> None:
    assert render_name("prop_{sub}_{name}_{v}", "Wooden Barrel!", "props", "containers", "model3d", "a") == \
        "prop_containers_wooden_barrel_a"
    assert validate_template("{category}/{asset_id}/{filename}", path=True) is None
    assert validate_template("{name.__class__}") is not None and validate_template("a/{name}") is not None
    assert safe_path_component("..") and safe_path_component("con") and safe_path_component("ok.png") is None
    assert collision_key("Café") == collision_key("CAFÉ")


def test_shotlist_parsers_report_lines() -> None:
    csv = shotlist_io.parse("s.csv", b"name,category,type,brief,prio\nCrate,props,model3d,wood,high\n,props,x,,zzz\n")
    for r in csv.rows:
        shotlist_io.validate_row(r, {"props"}, {"props": "model3d"})
    assert csv.rows[0].errors == [] and csv.rows[0].values["kind"] == "model3d"
    assert csv.rows[1].line == 3 and any("name" in e for e in csv.rows[1].errors)
    md = shotlist_io.parse("s.md", b"| name | kind |\n|---|---|\n| Coin | icon |\n")
    assert md.rows[0].values == {"name": "Coin", "kind": "icon"}
    prose = shotlist_io.parse("s.md", b"Please make a crate and a barrel")
    assert prose.errors and "table" in prose.errors[0]
    yml = shotlist_io.parse("s.yaml", b"items:\n  - name: A\n    name: B\n")
    assert yml.errors and "duplicate" in yml.errors[0]
