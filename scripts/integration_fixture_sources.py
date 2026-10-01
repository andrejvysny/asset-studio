"""Source material for the Godot integration v1 fixtures: Godot text files, GLBs, textures, ZIP writer.

Everything is generated in code from fixed inputs so the committed bytes are reproducible.
"""
from __future__ import annotations

import io
import json
import re
import struct
import zlib
from dataclasses import dataclass

import numpy as np
import trimesh

SERVER = "6f1c2a52-3c2e-4d4b-9a57-0b6f6f0c1d2e"
LIB_A, LIB_B = "prj_0000000000000001", "prj_0000000000000002"
IDENT = "Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, {x}, {y}, {z})"
ZIP_DATE = (1980, 1, 1, 0, 0, 0)
MEDIA = {".tscn": "text/x-godot-scene", ".tres": "text/x-godot-resource", ".gdshader": "text/x-godot-shader",
         ".gdshaderinc": "text/x-godot-shader", ".png": "image/png", ".glb": "model/gltf-binary"}


def at(x: float, y: float, z: float) -> str:
    return IDENT.format(x=x, y=y, z=z)


def _b(text: str) -> bytes:
    return text.encode()


# ---------------------------------------------------------------- Godot text files

def prop_files() -> dict[str, bytes]:
    return {
        "materials/prop_mat.tres": _b(
            '[gd_resource type="StandardMaterial3D" format=3 uid="uid://b3k1m0pa1n7ro"]\n\n[resource]\n'
            "albedo_color = Color(0.8, 0.3, 0.2, 1)\nroughness = 0.7\n"),
        "scenes/prop.tscn": _b(
            '[gd_scene load_steps=3 format=3 uid="uid://c8w2q1prop001"]\n\n'
            '[ext_resource type="StandardMaterial3D" uid="uid://b3k1m0pa1n7ro" path="res://materials/prop_mat.tres" '
            'id="1_mat"]\n\n[sub_resource type="BoxMesh" id="BoxMesh_1"]\nsize = Vector3(1, 0.5, 2)\n\n'
            '[node name="PrimitiveProp" type="Node3D"]\n\n'
            f'[node name="Body" type="MeshInstance3D" parent="."]\ntransform = {at(0, 0.25, 0)}\n'
            'mesh = SubResource("BoxMesh_1")\nsurface_material_override/0 = ExtResource("1_mat")\n\n'
            f'[node name="GroundAnchor" type="Marker3D" parent="."]\ntransform = {at(0.25, 0, -0.5)}\n')}


def prop_v2_files() -> dict[str, bytes]:
    return {"scenes/prop.tscn": _b(
        '[gd_scene load_steps=5 format=3 uid="uid://c8w2q1prop002"]\n\n'
        '[sub_resource type="BoxMesh" id="BoxMesh_crate"]\nsize = Vector3(1, 0.4, 2)\n\n'
        '[sub_resource type="BoxMesh" id="BoxMesh_lid"]\nsize = Vector3(1, 0.1, 2)\n\n'
        '[sub_resource type="StandardMaterial3D" id="StandardMaterial3D_crate"]\nalbedo_color = '
        'Color(0.55, 0.38, 0.2, 1)\n\n'
        '[sub_resource type="StandardMaterial3D" id="StandardMaterial3D_lid"]\nalbedo_color = '
        'Color(0.35, 0.25, 0.15, 1)\n\n'
        '[node name="PrimitiveProp" type="Node3D"]\n\n'
        f'[node name="Crate" type="MeshInstance3D" parent="."]\ntransform = {at(0, 0.2, 0)}\n'
        'mesh = SubResource("BoxMesh_crate")\nsurface_material_override/0 = SubResource("StandardMaterial3D_crate")\n\n'
        f'[node name="Lid" type="MeshInstance3D" parent="."]\ntransform = {at(0, 0.45, 0)}\n'
        'mesh = SubResource("BoxMesh_lid")\nsurface_material_override/0 = SubResource("StandardMaterial3D_lid")\n\n'
        f'[node name="GroundAnchor" type="Marker3D" parent="."]\ntransform = {at(0, 0, 0.5)}\n')}


def tree_files() -> dict[str, bytes]:
    def mat(uid: str, tex_uid: str, tex: str, extra: str) -> bytes:
        return _b(
            f'[gd_resource type="StandardMaterial3D" load_steps=2 format=3 uid="uid://{uid}"]\n\n'
            f'[ext_resource type="Texture2D" uid="uid://{tex_uid}" path="res://textures/{tex}.png" id="1_tex"]\n\n'
            f'[resource]\n{extra}albedo_texture = ExtResource("1_tex")\n')

    return {
        "materials/bark.tres": mat("b3k1m0bark001", "d4t8x1bark001", "bark", "roughness = 0.9\n"),
        "materials/foliage.tres": mat("b3k1m0leaf001", "d4t8x1leaf001", "foliage",
                                      "transparency = 2\nalpha_scissor_threshold = 0.5\ncull_mode = 2\n"),
        "textures/bark.png": png_bytes("bark"), "textures/foliage.png": png_bytes("foliage"),
        "scenes/tree.tscn": _b(
            '[gd_scene load_steps=5 format=3 uid="uid://c8w2q1tree001"]\n\n'
            '[ext_resource type="StandardMaterial3D" uid="uid://b3k1m0bark001" '
            'path="res://materials/bark.tres" id="1_bark"]\n'
            '[ext_resource type="StandardMaterial3D" uid="uid://b3k1m0leaf001" '
            'path="res://materials/foliage.tres" id="2_leaf"]\n\n'
            '[sub_resource type="CylinderMesh" id="CylinderMesh_trunk"]\ntop_radius = 0.15\nbottom_radius = '
            '0.25\nheight = 2.0\n\n'
            '[sub_resource type="SphereMesh" id="SphereMesh_crown"]\nradius = 1.0\nheight = 2.0\n\n'
            '[node name="TexturedTree" type="Node3D"]\n\n'
            f'[node name="Trunk" type="MeshInstance3D" parent="."]\ntransform = {at(0, 1, 0)}\n'
            'mesh = SubResource("CylinderMesh_trunk")\nsurface_material_override/0 = ExtResource("1_bark")\n\n'
            f'[node name="Crown" type="MeshInstance3D" parent="."]\ntransform = {at(0, 3, 0)}\n'
            'mesh = SubResource("SphereMesh_crown")\nsurface_material_override/0 = ExtResource("2_leaf")\n\n'
            '[node name="GroundAnchor" type="Marker3D" parent="."]\n')}


def rock_files() -> dict[str, bytes]:
    return {
        "models/rock.glb": rock_glb(),
        "scenes/rock.tscn": _b(
            '[gd_scene load_steps=3 format=3 uid="uid://c8w2q1rock001"]\n\n'
            '[ext_resource type="PackedScene" uid="uid://e5r2k1rock001" path="res://models/rock.glb" id="1_glb"]\n\n'
            '[sub_resource type="BoxShape3D" id="BoxShape3D_1"]\nsize = Vector3(1.2, 0.8, 1)\n\n'
            '[node name="VertexColorRock" type="Node3D"]\n\n'
            '[node name="Mesh" parent="." instance=ExtResource("1_glb")]\n\n'
            '[node name="Body" type="StaticBody3D" parent="."]\n\n'
            f'[node name="Shape" type="CollisionShape3D" parent="Body"]\ntransform = {at(0, 0.4, 0)}\n'
            'shape = SubResource("BoxShape3D_1")\n\n'
            '[node name="GroundAnchor" type="Marker3D" parent="."]\n')}


def hut_files() -> dict[str, bytes]:
    return {"scenes/hut.tscn": _b(
        '[gd_scene load_steps=3 format=3 uid="uid://c8w2q1hut0001"]\n\n'
        '[sub_resource type="StandardMaterial3D" id="StandardMaterial3D_wall"]\nalbedo_color = '
        'Color(0.75, 0.68, 0.55, 1)\n\n'
        '[sub_resource type="StandardMaterial3D" id="StandardMaterial3D_roof"]\nalbedo_color = '
        'Color(0.45, 0.2, 0.15, 1)\n\n'
        '[node name="CsgHut" type="Node3D"]\n\n[node name="Shell" type="CSGCombiner3D" parent="."]\n\n'
        f'[node name="Walls" type="CSGBox3D" parent="Shell"]\ntransform = {at(0, 1, 0)}\n'
        'size = Vector3(3, 2, 3)\nmaterial = SubResource("StandardMaterial3D_wall")\n\n'
        f'[node name="Door" type="CSGBox3D" parent="Shell"]\ntransform = {at(0, 0.75, 1.5)}\n'
        "operation = 2\nsize = Vector3(1, 1.5, 0.6)\n\n"
        f'[node name="Roof" type="CSGCylinder3D" parent="Shell"]\ntransform = {at(0, 2.375, 0)}\n'
        'radius = 2.2\nheight = 0.75\nsides = 4\nmaterial = SubResource("StandardMaterial3D_roof")\n\n'
        '[node name="GroundAnchor" type="Marker3D" parent="."]\n')}


def crystal_files() -> dict[str, bytes]:
    return {
        "shaders/common.gdshaderinc": _b(
            "float fresnel_glow(vec3 normal, vec3 view, float power) {\n"
            "\treturn pow(1.0 - clamp(dot(normalize(normal), normalize(view)), 0.0, 1.0), power);\n}\n"),
        "shaders/crystal.gdshader": _b(
            'shader_type spatial;\n#include "res://shaders/common.gdshaderinc"\n\n'
            "uniform vec4 tint : source_color = vec4(0.4, 0.8, 1.0, 1.0);\n\nvoid fragment() {\n"
            "\tALBEDO = tint.rgb;\n\tEMISSION = fresnel_glow(NORMAL, VIEW, 3.0) * tint.rgb;\n}\n"),
        "materials/crystal_mat.tres": _b(
            '[gd_resource type="ShaderMaterial" load_steps=2 format=3 uid="uid://b3k1m0cryst01"]\n\n'
            '[ext_resource type="Shader" uid="uid://f6s3h1cryst01" path="res://shaders/crystal.gdshader" id="1_sh"]\n\n'
            '[resource]\nshader = ExtResource("1_sh")\nshader_parameter/tint = Color(0.4, 0.8, 1, 1)\n'),
        "scenes/crystal.tscn": _b(
            '[gd_scene load_steps=3 format=3 uid="uid://c8w2q1cryst01"]\n\n'
            '[ext_resource type="ShaderMaterial" uid="uid://b3k1m0cryst01" '
            'path="res://materials/crystal_mat.tres" id="1_mat"]\n\n'
            '[sub_resource type="PrismMesh" id="PrismMesh_1"]\nsize = Vector3(1, 2, 1)\n\n'
            '[node name="Crystal" type="Node3D"]\n\n'
            f'[node name="Gem" type="MeshInstance3D" parent="."]\ntransform = {at(0, 1, 0)}\n'
            'mesh = SubResource("PrismMesh_1")\nsurface_material_override/0 = ExtResource("1_mat")\n\n'
            '[node name="GroundAnchor" type="Marker3D" parent="."]\n')}


def cluster_files() -> dict[str, bytes]:
    return {"scenes/cluster.tscn": _b(
        '[gd_scene load_steps=3 format=3 uid="uid://c8w2q1clust01"]\n\n'
        '[ext_resource type="PackedScene" uid="uid://c8w2q1prop001" '
        'path="res://deps/primitive_prop.glb" id="1_prop"]\n\n'
        '[sub_resource type="BoxMesh" id="BoxMesh_base"]\nsize = Vector3(4, 0.05, 2)\n\n'
        '[node name="PropCluster" type="Node3D"]\n\n'
        f'[node name="Base" type="MeshInstance3D" parent="."]\ntransform = {at(0, 0.025, 0)}\nmesh = '
        f'SubResource("BoxMesh_base")\n\n'
        f'[node name="PropA" parent="." instance=ExtResource("1_prop")]\ntransform = {at(-1.5, 0, 0)}\n\n'
        f'[node name="PropB" parent="." instance=ExtResource("1_prop")]\ntransform = {at(1.5, 0, 0)}\n\n'
        '[node name="GroundAnchor" type="Marker3D" parent="."]\n')}


# ---------------------------------------------------------------- binary assets

def _chunk(tag: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))


def stored_zlib(raw: bytes) -> bytes:
    """zlib stream of STORED deflate blocks: no compressor involved, so bytes are identical on every zlib build."""
    blocks = [raw[i:i + 65535] for i in range(0, len(raw), 65535)] or [b""]
    out = bytearray(b"\x78\x01")
    for i, blk in enumerate(blocks):
        out += struct.pack("<BHH", int(i == len(blocks) - 1), len(blk), len(blk) ^ 0xFFFF) + blk
    return bytes(out) + struct.pack(">I", zlib.adler32(raw))


def write_png(width: int, height: int, rgba: list[tuple[int, int, int, int]]) -> bytes:
    """8-bit RGBA PNG, filter 0, IDAT of stored deflate blocks only."""
    rows = b"".join(b"\x00" + bytes(c for px in rgba[y * width:(y + 1) * width] for c in px) for y in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", stored_zlib(rows))
            + _chunk(b"IEND", b""))


def png_bytes(kind: str) -> bytes:
    px = []
    for y in range(32):
        for x in range(32):
            if kind == "bark":
                shade = 70 + (x * 7 % 29) + (y % 4) * 6
                px.append((shade + 40, shade + 10, shade - 20, 255))
            else:
                inside = (x - 15.5) ** 2 + (y - 15.5) ** 2 < 14**2 and (x + y) % 5 != 0
                px.append((40 + y * 3, 120 + x * 2, 50, 255 if inside else 0))
    return write_png(32, 32, px)


def _glb(scene: trimesh.Scene | trimesh.Trimesh) -> bytes:
    buf = io.BytesIO()
    scene.export(buf, file_type="glb")
    return buf.getvalue()


def _box(extents: tuple[float, float, float], y: float) -> trimesh.Trimesh:
    return trimesh.creation.box(extents=extents, transform=trimesh.transformations.translation_matrix((0, y, 0)))


def prop_glb() -> bytes:
    return _glb(_box((1, 0.5, 2), 0.25))


def prop_v2_glb() -> bytes:
    scene = trimesh.Scene()
    scene.add_geometry(_box((1, 0.4, 2), 0.2), geom_name="crate", node_name="Crate")
    scene.add_geometry(_box((1, 0.1, 2), 0.45), geom_name="lid", node_name="Lid")
    return _glb(scene)


def rock_mesh() -> trimesh.Trimesh:
    mesh = trimesh.creation.icosphere(subdivisions=1)
    mesh.apply_scale((0.6, 0.4, 0.5))
    mesh.apply_translation((0, 0.4, 0))
    n = len(mesh.vertices)
    colors = np.zeros((n, 4), dtype=np.uint8)
    colors[:, 0] = np.linspace(90, 160, n).astype(np.uint8)
    colors[:, 1] = np.linspace(80, 130, n).astype(np.uint8)
    colors[:, 2] = 70
    colors[:, 3] = 255
    mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh, vertex_colors=colors)
    return mesh


def rock_glb() -> bytes:
    return _glb(rock_mesh())


def glb_with_external_uri() -> bytes:
    doc = json.dumps({"asset": {"version": "2.0"}, "buffers": [{"uri": "external.bin", "byteLength": 4}]},
                     separators=(",", ":")).encode()
    doc += b" " * (-len(doc) % 4)
    body = struct.pack("<I4s", len(doc), b"JSON") + doc
    return struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body


# ---------------------------------------------------------------- resource references

_ATTR = re.compile(r'(\w+)="([^"]*)"')
_EXT = re.compile(r"^\[ext_resource\s[^\n]*\]$", re.M)
_INCLUDE = re.compile(r'^\s*#include\s+"(res://[^"]*)"', re.M)


def scan_references(files: dict[str, bytes]) -> dict[str, str | None]:
    """res:// path -> uid (or None) for every ext_resource and shader #include, as written (unresolved ones too)."""
    refs: dict[str, str | None] = {}
    for name, data in sorted(files.items()):
        text = data.decode("utf-8", "replace") if name.endswith((".tscn", ".tres", ".gdshader", ".gdshaderinc")) else ""
        for line in _EXT.findall(text):
            attrs = dict(_ATTR.findall(line))
            if "path" in attrs:
                refs[attrs["path"]] = attrs.get("uid")
        for path in _INCLUDE.findall(text):
            refs.setdefault(path, None)
    return refs


# ---------------------------------------------------------------- zip

@dataclass(frozen=True)
class Member:
    name: str
    data: bytes
    mode: int = 0o644
    encrypted: bool = False
    deflate_zeros: bool = False  # data is all zeros: store as a hand-built deflate stream (the zip-bomb fixture)


def _bits(pairs: list[tuple[int, int]]) -> bytes:
    """Pack (value, nbits) LSB-first, as deflate does; Huffman codes must already be bit-reversed."""
    acc = n = 0
    for value, nbits in pairs:
        acc |= value << n
        n += nbits
    return acc.to_bytes((n + 7) // 8, "little")


def deflate_zeros(count: int) -> bytes:
    """Raw deflate of `count` zero bytes: one dynamic-Huffman block, hand-encoded (zlib-independent, ~1000:1).

    Literal tree: sym 285 (len 258) -> '0', sym 0 -> '10', sym 256 -> '11'. Distance tree: one 1-bit code (dist 1).
    Huffman codes are packed bit-reversed (deflate sends them MSB first inside an LSB-first stream).
    """
    assert count >= 1
    cl_order = (16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15)
    cl_len = {0: 2, 1: 2, 2: 2, 18: 2}  # code-length alphabet, canonical codes 00, 01, 10, 11 (reversed below)
    cl_code = {0: 0b00, 1: 0b10, 2: 0b01, 18: 0b11}
    # lengths: lit 0=2, 1..255=0, 256=2, 257..284=0, 285=1, then the single distance code=1 (HLIT=29, HDIST=0)
    seq = [(2, 0, 0), (18, 127, 7), (18, 106, 7), (2, 0, 0), (18, 17, 7), (1, 0, 0), (1, 0, 0)]
    pairs = [(1, 1), (2, 2), (29, 5), (0, 5), (14, 4)]
    pairs += [(cl_len.get(sym, 0), 3) for sym in cl_order[:18]]
    for sym, extra, ebits in seq:
        pairs.append((cl_code[sym], 2))
        if ebits:
            pairs.append((extra, ebits))
    literal, eob, match = (0b01, 2), (0b11, 2), (0, 1)
    matches, rest = divmod(count - 1, 258)
    pairs.append(literal)
    pairs += [match, (0, 1)] * matches  # length 258 (no extra bits) at distance 1 (code 0)
    pairs += [literal] * rest + [eob]
    return _bits(pairs)


def _zip_entry(m: Member) -> tuple[bytes, int, int, int]:
    """(stored payload, method, crc32, uncompressed size)"""
    crc = zlib.crc32(m.data)
    if m.deflate_zeros:
        assert not any(m.data), "deflate_zeros members must be all zeros"
        return deflate_zeros(len(m.data)), 8, crc, len(m.data)
    return m.data, 0, crc, len(m.data)


def build_zip(members: list[Member]) -> bytes:
    """Hand-assembled ZIP: ZIP_STORED members (and the one hand-deflated bomb), so no zlib compressor is involved."""
    out = bytearray()
    central = bytearray()
    ordered = sorted(members, key=lambda x: x.name)
    for m in ordered:
        name = m.name.encode("utf-8")
        flag = (0x800 if not m.name.isascii() else 0) | (0x01 if m.encrypted else 0)
        payload, method, crc, size = _zip_entry(m)
        offset = len(out)
        out += struct.pack("<4sHHHHHIIIHH", b"PK\x03\x04", 20, flag, method, 0, 33, crc, len(payload), size,
                           len(name), 0) + name + payload
        central += struct.pack("<4sHHHHHHIIIHHHHHII", b"PK\x01\x02", (3 << 8) | 20, 20, flag, method, 0, 33, crc,
                               len(payload), size, len(name), 0, 0, 0, 0, m.mode << 16, offset) + name
    out_len = len(out)
    out += central + struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, len(ordered), len(ordered), len(central),
                                 out_len, 0)
    return bytes(out)
