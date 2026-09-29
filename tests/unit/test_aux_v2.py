"""aux v2: pure parsers (service), AuxClient payloads (MockTransport), FakeAux behaviour. All offline."""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import httpx
import pytest
from assetstudio_server.adapters.aux import AuxClient
from assetstudio_server.adapters.fake import FakeAux

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "prompt_service"))
import aux_v2  # noqa: E402  (service module without torch)


def J(**kw: object) -> str:
    return "Sure:\n" + json.dumps(kw)


# ---- enhance -------------------------------------------------------------------------------------------------

def test_enhance_conservative_drops_additions() -> None:
    out = aux_v2.parse_enhance_output(J(description="A crate", additions=["glowing runes"], assumptions=["oak"]),
                                      "conservative")
    assert out["additions"] == [] and out["assumptions"] == ["oak", aux_v2.DROPPED_NOTE]


def test_enhance_creative_keeps_additions() -> None:
    out = aux_v2.parse_enhance_output(J(description="A crate", additions=["runes"]), "creative")
    assert out["additions"] == ["runes"] and out["assumptions"] == []


@pytest.mark.parametrize("raw", ["no json", J(description=""), J(description=3), J(description="x", tags="a"),
                                 J(description="x", reference_cues=[{"index": "0", "cue": "c"}]),
                                 J(description="x", additions=[1]), "[1]"])
def test_enhance_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        aux_v2.parse_enhance_output(raw, "creative")


def test_enhance_cues_bounds() -> None:
    out = aux_v2.parse_enhance_output(J(description="x", reference_cues=[{"index": 0, "cue": "a"},
                                                                          {"index": 5, "cue": "b"}]), "creative", 2)
    assert out["reference_cues"] == [{"index": 0, "cue": "a"}]


# ---- compare -------------------------------------------------------------------------------------------------

def test_compare_strict() -> None:
    raw = J(checks={"a": True, "b": "false", "c": "unsure", "d": False}, reasons={"a": "ok", "d": "no"})
    out = aux_v2.parse_compare(raw, ["a", "b", "c", "d", "e"])
    assert out["checks"] == {"a": True, "b": "unsure", "c": "unsure", "d": False, "e": "unsure"}
    assert out["reasons"]["e"] == "not answered" and out["reasons"]["d"] == "no"
    assert out["reasons"]["b"].startswith("invalid answer")


@pytest.mark.parametrize("raw", ["garbage", "{bad json", J(checks=[True]), J(checks=None)])
def test_compare_malformed_is_unsure(raw: str) -> None:
    out = aux_v2.parse_compare(raw, ["a"])
    assert out["checks"] == {"a": "unsure"} and out["reasons"] == {"a": "not answered"}


# ---- analysis ------------------------------------------------------------------------------------------------

def test_analysis_index_bounds_and_slugs() -> None:
    raw = J(observations=[{"text": "green cone", "images": [1, 2, 9]}, {"text": "uncited", "images": [7]},
                          {"text": "zero", "images": [0]}],
            uncertainties=["species"], proposed_preserve=[{"id": "Tree Shape!", "text": "cone"},
                                                          {"id": "tree shape", "text": "cone 2"},
                                                          {"id": "", "text": "Trunk colour"}],
            proposed_changeable=["height"])
    out = aux_v2.parse_analysis(raw, 2)
    assert out["observations"] == [{"text": "green cone", "images": [0, 1]}]
    assert out["dropped_observations"] == 2
    assert [p["id"] for p in out["proposed_preserve"]] == ["tree_shape", "tree_shape_2", "trunk_colour"]


@pytest.mark.parametrize("raw", ["x", J(observations="a"), J(observations=[{"text": "a", "images": ["1"]}]),
                                 J(observations=[], uncertainties="u")])
def test_analysis_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        aux_v2.parse_analysis(raw, 1)


# ---- suggestions ---------------------------------------------------------------------------------------------

def test_suggestions_truncate_dedupe_clip() -> None:
    rows = [{"label": "Tall", "change_request": "a"}, {"label": "tall", "change_request": "b"},
            {"label": "L" * 100, "change_request": "c" * 900}, {"label": "Extra", "change_request": "d"}]
    out = aux_v2.parse_suggestions(J(rows=rows, notes=["n"]), 3)
    assert [r["label"][:4] for r in out["rows"]] == ["Tall", "tall", "LLLL"]
    assert out["rows"][1]["label"] == "tall 2" and len(out["rows"][2]["label"]) <= 60
    assert len(out["rows"][2]["change_request"]) == 400 and out["short_by"] == 0 and out["notes"] == ["n"]


def test_suggestions_short_by_and_duplicate_suffix_fits() -> None:
    out = aux_v2.parse_suggestions(J(rows=[{"label": "A" * 60, "change_request": "x"}] * 2), 5)
    assert out["short_by"] == 3
    assert len({r["label"] for r in out["rows"]}) == 2 and all(len(r["label"]) <= 60 for r in out["rows"])


@pytest.mark.parametrize("raw", ["x", J(rows=[]), J(rows="a"), J(rows=[{"label": 1, "change_request": "x"}])])
def test_suggestions_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        aux_v2.parse_suggestions(raw, 2)


def test_request_models_bounds() -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        aux_v2.SuggestRequest(request="x", count=33)
    with pytest.raises(ValidationError):
        aux_v2.CompareRequest(images=[aux_v2.CompareImage(b64="", label="source")],
                              questions=[aux_v2.CompareQuestion(id="a", question="q")])
    with pytest.raises(ValidationError):
        aux_v2.RefImage(b64="", note="n" * 501)


# ---- client --------------------------------------------------------------------------------------------------

def _client(seen: list[httpx.Request], body: dict | None = None) -> AuxClient:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=body or {"ok": True})
    return AuxClient("http://aux", httpx.Client(base_url="http://aux", transport=httpx.MockTransport(handler)))


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def test_client_new_endpoints_payloads() -> None:
    seen: list[httpx.Request] = []
    c = _client(seen)
    c.compare(images=[(b"s", "source", ""), (b"c", "candidate", "n")], questions=[("q1", "Same?")], context="ctx",
              epoch=7, execution_id="ex1")
    c.analyze_source(images=[(b"a", "front")], kind="prop", user_facts="f", epoch=7)
    c.suggest_variants(images=[(b"a", "front")], request="r", count=3, intent="subtle", preserve="p", kind="prop",
                       observations=["o"], epoch=7)
    c.suggest_variants(images=[], request="r", count=1, intent="related", preserve="", kind="prop", epoch=7)
    paths = [r.url.path for r in seen]
    assert paths == ["/compare", "/analyze_source", "/suggest_variants", "/suggest_variants"]
    assert seen[0].headers["x-lease-epoch"] == "7" and seen[0].headers["x-execution-id"] == "ex1"
    assert "x-execution-id" not in seen[1].headers
    p = [json.loads(r.content) for r in seen]
    assert p[0] == {"images": [{"b64": _b64(b"s"), "label": "source", "note": ""},
                               {"b64": _b64(b"c"), "label": "candidate", "note": "n"}],
                    "questions": [{"id": "q1", "question": "Same?"}], "context": "ctx"}
    assert p[1] == {"images": [{"b64": _b64(b"a"), "view": "front"}], "kind": "prop", "user_facts": "f"}
    assert p[2]["observations"] == ["o"] and p[2]["count"] == 3 and p[2]["intent"] == "subtle"
    assert p[3]["images"] == [] and p[3]["observations"] == []


def test_client_enhance_legacy_and_v2() -> None:
    seen: list[httpx.Request] = []
    c = _client(seen)
    c.enhance(brief="b", kind="prop", constraints="c", style_guide="s", epoch=1)
    c.enhance(brief="b", kind="prop", constraints="c", style_guide="s", epoch=1, preset="creative", mode="edit",
              images=[(b"i", "source", "the src")], preserve="p", change="taller")
    old, new = (json.loads(r.content) for r in seen)
    assert set(old) == {"brief", "kind", "constraints", "style_guide"}
    assert new["preset"] == "creative" and new["mode"] == "edit" and new["change"] == "taller"
    assert new["images"] == [{"b64": _b64(b"i"), "role": "source", "note": "the src"}]


# ---- FakeAux -------------------------------------------------------------------------------------------------

def test_fake_enhance() -> None:
    f = FakeAux()
    f.gpu.lease(1)
    e = f.enhance(brief="tall tree", kind="prop", constraints="", style_guide="", epoch=1, mode="edit",
                  preset="creative", change="make taller", preserve="trunk",
                  images=[(b"s", "source", ""), (b"r", "reference", "needle shape")])
    assert e["description"].startswith("Edit the source object: make taller. Keep: trunk.")
    assert e["additions"] == ["simulated addition: finer surface detail"] and e["meta"]["simulated"] is True
    assert e["reference_cues"] == [{"index": 0, "cue": "needle shape"}]
    e = f.enhance(brief="x", kind="prop", constraints="", style_guide="", epoch=1)
    assert e["additions"] == [] and e["reference_cues"] == []


def test_fake_compare_analyze_suggest() -> None:
    f = FakeAux()
    f.gpu.lease(1)
    qs = [("resemblance", "?"), ("change", "?")]
    same = f.compare(images=[(b"a", "source", ""), (b"a", "candidate", "")], questions=qs, context="", epoch=1)
    diff = f.compare(images=[(b"a", "source", ""), (b"b", "candidate", "")], questions=qs, context="", epoch=1)
    assert same["checks"] == {"resemblance": True, "change": False}
    assert diff["checks"] == {"resemblance": True, "change": True} and set(diff["reasons"]) == {"resemblance", "change"}
    a = f.analyze_source(images=[(b"a", "front")], kind="prop", epoch=1)
    assert a["proposed_preserve"] == [{"id": "preserve_identity", "text": "Keep the same asset identity."}]
    assert a["meta"]["simulated"] is True
    s = f.suggest_variants(images=[], request="r", count=8, intent="related", preserve="", kind="prop", epoch=1)
    labels = [r["label"] for r in s["rows"]]
    assert len(labels) == 8 == len(set(labels)) and labels[:2] == ["Taller", "Squat"] and labels[6] == "Taller 2"
