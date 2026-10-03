"""Bounded Godot text-resource parser: fixtures, value grammar, limits, error locations."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from assetstudio_processing import godot_text
from assetstudio_processing.godot_text import Call, GodotTextError, Num, Ref, parse_godot_text

VALID = Path(__file__).resolve().parents[2] / "contracts/godot-integration/v1/fixtures/source_packages/valid"
HEAD = b"[gd_resource type=\"StandardMaterial3D\" format=3]\n\n"


def _text_members() -> list[tuple[str, bytes]]:
    out = []
    for z in sorted(VALID.glob("*.zip")):
        with zipfile.ZipFile(z) as zf:
            out += [(f"{z.stem}:{n}", zf.read(n)) for n in zf.namelist() if n.endswith((".tscn", ".tres"))]
    return out


@pytest.mark.parametrize(("label", "data"), _text_members(), ids=lambda v: v if isinstance(v, str) else "")
def test_valid_fixture_text_resources_parse(label: str, data: bytes) -> None:
    doc = parse_godot_text(data)
    assert doc.kind in ("gd_scene", "gd_resource") and doc.header["format"] in (Num("3"), Num("4"))
    assert {r.id for r in doc.refs()} <= {s.attrs["id"] for s in doc.ext_resources() + doc.sub_resources()}


def test_scene_structure_and_property_keys() -> None:
    data = _text_members()
    prop = next(d for label, d in data if label == "primitive_prop:scenes/prop.tscn")
    doc = parse_godot_text(prop)
    assert [n.attrs["name"] for n in doc.nodes()] == ["PrimitiveProp", "Body", "GroundAnchor"]
    body = doc.nodes()[1]
    keys = dict(body.props)
    assert keys["surface_material_override/0"] == Ref("ExtResource", "1_mat")
    assert keys["mesh"] == Ref("SubResource", "BoxMesh_1")
    assert isinstance(keys["transform"], Call) and keys["transform"].name == "Transform3D"
    assert doc.ext_resources()[0].attrs["path"] == "res://materials/prop_mat.tres"


def test_values_constructors_arrays_dicts_and_escapes() -> None:
    src = HEAD + (
        b'[resource]\n'
        b'a = Vector3(1, -2.5e-3, inf)\n'
        b'metadata/x = { "k": [1, 2, {"n": null}], "t": true }\n'
        b'arr = PackedStringArray("a", "b\\n\\"q\\"\\u00e9")\n'
        b'multi = "line1\n  line2 ; not a comment"\n'
        b'; a comment\n'
        b'name = &"sn"\npath = ^"A/B"\nneg = -inf\ntyped = Array[int]([1])\n'
        b'ext = ExtResource("1")\r\nhex = 0x1F\n')
    props = dict(parse_godot_text(src).sections[0].props)
    assert props["a"] == Call("Vector3", (Num("1"), Num("-2.5e-3"), Num("inf")))
    assert props["metadata/x"] == {"k": [Num("1"), Num("2"), {"n": None}], "t": True}
    assert props["arr"] == Call("PackedStringArray", ("a", 'b\n"q"\u00e9'))
    assert props["multi"] == "line1\n  line2 ; not a comment"
    assert props["name"] == Call("StringName", ("sn",)) and props["path"] == Call("NodePath", ("A/B",))
    assert props["neg"] == Num("-inf") and props["typed"] == Call("Array[int]", ([Num("1")],))
    assert props["ext"] == Ref("ExtResource", "1") and props["hex"] == Num("0x1F")


def test_crlf_input_and_header_attrs() -> None:
    doc = parse_godot_text(b'[gd_scene load_steps=2 format=3 uid="uid://abc"]\r\n\r\n[node name="R" type="Node3D"]\r\n')
    assert doc.header["uid"] == "uid://abc" and doc.nodes()[0].line == 3


@pytest.mark.parametrize(("src", "line"), [
    (HEAD + b"[resource]\nx = Vector3(1, 2\n", None),
    (HEAD + b"[resource]\nx = 1 y = 2\n", 4),
    (HEAD + b'[resource]\nx = "open\n', 4),
    (HEAD + b"[resource]\nx = bareword\n", 4),
    (HEAD + b"[resource]\nx = ExtResource()\n", 4),
    (HEAD + b"[resource]\nx = {[1]: 2}\n", 4),
    (HEAD + b'[resource]\nx = "\\uD800"\n', 4),
    (b"[resource]\n", 1),
    (b"x = 1\n", None),
    (b'[gd_resource type="A"]\n', 1),
])
def test_malformed_input_reports_parse_error(src: bytes, line: int | None) -> None:
    with pytest.raises(GodotTextError) as e:
        parse_godot_text(src)
    assert e.value.code == "unsafe_package" and e.value.detail in ("parse_error", "unsupported_format")
    if line is not None:
        assert e.value.line == line


def test_format_4_with_base64_packed_array_parses_and_other_formats_are_rejected() -> None:
    doc = parse_godot_text(b'[gd_scene format=4]\n\n[sub_resource type="ArrayMesh" id="A_1"]\n_surfaces = [{\n'
                           b'"aabb": AABB(-0.5, 1e-05, 0, 1, 1, 1),\n"vertex_data": PackedByteArray("AAAAvwAAAD8=")\n}]\n')
    assert doc.header["format"] == Num("4")
    surf = doc.sub_resources()[0].props[0][1][0]
    assert surf["vertex_data"] == Call("PackedByteArray", ("AAAAvwAAAD8=",))
    for bad in (b"[gd_scene format=5]\n", b"[gd_scene format=2]\n", b"[gd_scene]\n"):
        with pytest.raises(GodotTextError) as e:
            parse_godot_text(bad)
        assert e.value.detail == "unsupported_format"


def test_binary_and_non_utf8_rejected() -> None:
    with pytest.raises(GodotTextError) as e:
        parse_godot_text(b"RSRC\x00\x00")
    assert e.value.detail == "binary_resource"
    with pytest.raises(GodotTextError) as e:
        parse_godot_text(b"[gd_scene format=3]\n\xff\xfe")
    assert e.value.detail == "not_utf8"


def test_depth_limit() -> None:
    ok = HEAD + b"[resource]\nx = " + b"[" * 63 + b"]" * 63 + b"\n"
    parse_godot_text(ok)
    deep = HEAD + b"[resource]\nx = " + b"[" * 65 + b"]" * 65 + b"\n"
    with pytest.raises(GodotTextError) as e:
        parse_godot_text(deep)
    assert (e.value.code, e.value.detail) == ("resource_limit", "depth_limit")


def test_size_token_string_and_section_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(GodotTextError) as e:
        parse_godot_text(b" " * (godot_text.MAX_INPUT_BYTES + 1))
    assert e.value.code == "resource_limit"
    big = HEAD + b'[resource]\nx = "' + b"a" * (godot_text.MAX_STRING + 1) + b'"\n'
    with pytest.raises(GodotTextError) as e:
        parse_godot_text(big)
    assert e.value.detail == "string_limit"
    monkeypatch.setattr(godot_text, "MAX_TOKENS", 20)
    with pytest.raises(GodotTextError) as e:
        parse_godot_text(HEAD + b"[resource]\nx = [" + b"1," * 50 + b"1]\n")
    assert e.value.detail == "token_limit"
    monkeypatch.setattr(godot_text, "MAX_SECTIONS", 3)
    with pytest.raises(GodotTextError) as e:
        parse_godot_text(HEAD + b"[resource]\n" * 5)
    assert e.value.detail == "section_limit"
