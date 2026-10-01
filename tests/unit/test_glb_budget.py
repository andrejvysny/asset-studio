"""GLB budget measurements: triangles, nodes, materials, texture pixels, exceeded list."""
from __future__ import annotations

import io

import trimesh
from assetstudio_processing.glb_budget import glb_budget, image_size
from PIL import Image

LIMITS = {"ipad_glb_max_bytes": 134217728, "ipad_max_triangles": 200000, "ipad_max_nodes": 1024,
          "ipad_max_materials": 64, "ipad_max_texture_px": 4096}


def _glb(textured_px: int | None = None) -> bytes:
    mesh = trimesh.creation.box()
    if textured_px:
        img = Image.new("RGB", (textured_px, textured_px // 2), (10, 200, 30))
        mesh.visual = trimesh.visual.TextureVisuals(uv=mesh.vertices[:, :2] * 0 + 0.5, image=img)
    buf = io.BytesIO()
    mesh.export(buf, file_type="glb")
    return buf.getvalue()


def test_counts_for_plain_box() -> None:
    b = glb_budget(_glb(), LIMITS)
    assert b["triangles"] == 12 and b["nodes"] >= 1 and b["max_texture_px"] is None
    assert b["within_ipad_budget"] is True and b["exceeded"] == [] and b["glb_bytes"] > 0


def test_texture_px_read_from_png_header() -> None:
    b = glb_budget(_glb(128), LIMITS)
    assert b["max_texture_px"] == 128 and b["materials"] >= 1


def test_exceeded_lists_every_limit() -> None:
    tight = {**LIMITS, "ipad_max_triangles": 5, "ipad_max_texture_px": 64}
    b = glb_budget(_glb(128), tight)
    assert b["within_ipad_budget"] is False and set(b["exceeded"]) == {"triangles", "max_texture_px"}


def test_unreadable_glb_and_image_sizes() -> None:
    assert glb_budget(b"nope", LIMITS)["exceeded"] == ["unreadable"]
    for fmt, size in (("PNG", (30, 20)), ("JPEG", (31, 21)), ("WEBP", (32, 22))):
        out = io.BytesIO()
        Image.new("RGB", size, (1, 2, 3)).save(out, fmt)
        assert image_size(out.getvalue()) == size, fmt
    assert image_size(b"garbage") is None
