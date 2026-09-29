"""Variant inputs: normalised PNG prepared inputs (profile v2), source identity by content, honest constraint labels
and edit-workflow readiness. SIMULATED engines only."""
from __future__ import annotations

import io
import itertools
from typing import Any

import pytest
from assetstudio_core.domain import AssetManifest
from assetstudio_core.variants import ReferenceImage
from assetstudio_processing.images import inspect_image
from assetstudio_server.errors import ApiError
from assetstudio_server.services import runtime as runtime_svc
from assetstudio_server.services import variant_refs
from assetstudio_server.services.variant_gen import SourceIntegrityError, primary_bytes
from assetstudio_server.services.variants import resolve_source
from assetstudio_storage.project import manifest_key
from PIL import Image, ImageCms

from tests.conftest import Api, new_project, png_bytes
from tests.contract.test_api_library import _glb, _import
from tests.contract.test_api_variants import P, _create, _draft, _get, _png_src, _prepare
from tests.contract.test_api_variants_generate import _edit_requests, _run_and_confirm

BG = (200, 200, 200)
_COUNTER = itertools.count()


def _encode(im: Image.Image, fmt: str, **kw: Any) -> bytes:
    out = io.BytesIO()
    im.save(out, fmt, **kw)
    return out.getvalue()


def _prepared(api: Api, monkeypatch: pytest.MonkeyPatch, data: bytes) -> tuple[bytes, dict]:
    pid = new_project(api, f"P{next(_COUNTER)}")
    src = _png_src(api, pid)
    ctx = api.studio.registry.get(pid)
    monkeypatch.setattr(variant_refs, "primary_bytes", lambda _c, _s: data)
    out = variant_refs._prepare_2d(ctx, resolve_source(ctx, src["asset_id"], src["version_id"]))[0]
    png = ctx.store.artifact_bytes(out["artifact_id"])
    inspect_image(png, ("PNG",))  # the adapter's rule
    return png, out["params"]


def _rgb(png: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(png))
    assert im.mode == "RGB" and not im.info.get("icc_profile") and not im.getexif()
    return im


def _near(a: tuple[int, ...], b: tuple[int, ...], tol: int = 12) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def test_png_rgb_and_rgba_are_normalised(make_api, monkeypatch) -> None:
    api = make_api()
    png, meta = _prepared(api, monkeypatch, png_bytes(color=(10, 20, 30)))
    assert _rgb(png).getpixel((3, 3)) == (10, 20, 30) and meta["profile"] == "assetstudio.image_prepare.v2"
    assert meta["alpha_composited"] is False and meta["icc_converted"] is False and meta["exif_orientation"] is None
    rgba = Image.new("RGBA", (8, 8), (255, 0, 0, 255))
    rgba.paste((0, 0, 255, 0), (0, 0, 4, 8))
    png, meta = _prepared(api, monkeypatch, _encode(rgba, "PNG"))
    im = _rgb(png)
    assert im.getpixel((1, 1)) == BG and im.getpixel((6, 1)) == (255, 0, 0) and meta["alpha_composited"] is True
    assert meta["background"] == list(BG) and meta["source_mode"] == "RGBA"


def test_palette_transparency_and_la_are_composited(make_api, monkeypatch) -> None:
    api = make_api()
    pal = Image.new("P", (4, 4), 1)
    pal.putpalette([0, 0, 0, 255, 0, 0] + [0] * 250 * 3)
    png, _ = _prepared(api, monkeypatch, _encode(pal, "PNG", transparency=1))
    assert _rgb(png).getpixel((1, 1)) == BG
    la = Image.new("LA", (4, 4), (50, 0))
    png, _ = _prepared(api, monkeypatch, _encode(la, "PNG"))
    assert _rgb(png).getpixel((1, 1)) == BG


def test_jpeg_webp_and_exif_orientation(make_api, monkeypatch) -> None:
    api = make_api()
    png, meta = _prepared(api, monkeypatch, _encode(Image.new("RGB", (16, 8), (0, 0, 255)), "JPEG", quality=100))
    assert _near(_rgb(png).getpixel((4, 4)), (0, 0, 255)) and meta["source_format"] == "JPEG"
    png, meta = _prepared(api, monkeypatch, _encode(Image.new("RGB", (16, 8), (0, 255, 0)), "WEBP", lossless=True))
    assert _rgb(png).getpixel((4, 4)) == (0, 255, 0) and meta["source_format"] == "WEBP"
    src = Image.new("RGB", (16, 8), (0, 0, 255))
    src.paste((255, 0, 0), (0, 0, 8, 4))  # red block at the stored top-left
    exif = Image.Exif()
    exif[0x0112] = 6  # display rotated 90 deg clockwise: the stored top-left lands top-right
    png, meta = _prepared(api, monkeypatch, _encode(src, "JPEG", quality=100, exif=exif))
    im = _rgb(png)
    assert im.size == (8, 16) and (meta["width"], meta["height"], meta["exif_orientation"]) == (8, 16, 6)
    assert _near(im.getpixel((7, 0)), (255, 0, 0)) and _near(im.getpixel((0, 15)), (0, 0, 255))


def test_icc_profile_is_converted_and_bad_profile_fails_loudly(make_api, monkeypatch) -> None:
    api = make_api()
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    png, meta = _prepared(api, monkeypatch, _encode(Image.new("RGB", (8, 8), (90, 120, 150)), "JPEG", quality=100,
                                                    icc_profile=icc))
    assert meta["icc_converted"] is True and _near(_rgb(png).getpixel((3, 3)), (90, 120, 150))
    with pytest.raises(ApiError) as e:
        _prepared(api, monkeypatch, _encode(Image.new("RGB", (8, 8)), "JPEG", icc_profile=b"not a profile"))
    assert e.value.code == "reference_conditioning_unavailable" and "sRGB" in e.value.message


def test_jpeg_draft_flows_to_generation_with_png_input(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    jpg = _encode(Image.new("RGB", (32, 32), (200, 80, 40)), "JPEG")
    src = _import(api, pid, "Photo", jpg, "photo.jpg")
    d = _prepare(api, pid, _draft(api, pid, src, "image_edit", "draft-vi-1", change_request="rust it"))
    out = _create(api, pid, d, "jobs-vi-1")
    _run_and_confirm(api, pid, out["job_ids"][0], "vi")
    reqs = _edit_requests(api)
    assert reqs and all(r.image.startswith(b"\x89PNG") for r in reqs)


def test_primary_bytes_rejects_non_png_frozen_reference(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    ctx = api.studio.registry.get(pid)
    jpg = _encode(Image.new("RGB", (8, 8)), "JPEG")
    art = ctx.store.register_artifact(jpg, "source_prepared", "image/jpeg", meta={}, lineage=[])

    class Vs:
        primary = ReferenceImage(artifact_id=art.id, sha256=art.sha256, role="primary", view="image")

    with pytest.raises(SourceIntegrityError, match="predates PNG normalisation"):
        primary_bytes(ctx, Vs())  # type: ignore[arg-type]


def test_renaming_the_source_does_not_invalidate_a_draft(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    src = _import(api, pid, "Crate", _glb(), "crate.glb")
    d = _draft(api, pid, src, "image_edit_reconstruct", "draft-vr-1", change_request="rust it")
    ctx = api.studio.registry.get(pid)
    rev = ctx.store.get(manifest_key(src["asset_id"]), AssetManifest)[0].revision
    api.raw("PATCH", f"{P}/{pid}/assets/{src['asset_id']}", json={"expected_revision": rev, "display_name": "Renamed"})
    d = _prepare(api, pid, d)
    assert d["reference_set_id"]
    assert len(_create(api, pid, d, "jobs-vr-1")["job_ids"]) == 1


def test_machine_enforced_constraint_label_is_rejected(make_api) -> None:
    api = make_api()
    pid = new_project(api)
    d = _draft(api, pid, _import(api, pid, "Crate", _glb(), "crate.glb"), "image_edit_reconstruct", "draft-vc-1")
    url = f"{P}/{pid}/variant-drafts/{d['id']}"
    bad = api.raw("PATCH", url, json={"expected_revision": d["revision"], "preserve": [
        {"id": "keep_shape", "text": "Keep the shape", "enforcement": "machine_enforced"}]})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "enforcement_unsupported"
    ok = api.raw("PATCH", url, json={"expected_revision": d["revision"], "preserve": [
        {"id": "keep_shape", "text": "Keep the shape", "enforcement": "advisory_visual"}]})
    assert ok.status_code == 200 and _get(api, pid, d["id"])["preserve"][0]["enforcement"] == "advisory_visual"


def _edit_unready(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    report = {"reachable": True, "ready": True, "problems": [], "workflows": {
        "fake.t2i": {"kind": "t2i", "ready": True, "problems": []},
        "fake.image_edit": {"kind": "image_edit", "ready": False, "problems": ["missing node QwenEditNode"]}}}
    monkeypatch.setattr(api.studio.engine, "check", lambda: report)
    runtime_svc._CACHE.clear()


def test_edit_workflow_readiness_gates_generative_methods_and_materialisation(make_api, monkeypatch) -> None:
    api = make_api()
    pid = new_project(api)
    src = _import(api, pid, "Crate", _glb(), "crate.glb")
    d = _prepare(api, pid, _draft(api, pid, src, "image_edit_reconstruct", "draft-ve-1", change_request="rust it"))
    _edit_unready(api, monkeypatch)
    caps = api.get(f"{P}/{pid}/assets/{src['asset_id']}/versions/{src['version_id']}/variant-capabilities")
    by = {m["method"]: m for m in caps["methods"]}
    assert by["image_edit_reconstruct"]["available"] is False
    assert by["image_edit_reconstruct"]["reason"] == "engine_unavailable"
    assert "QwenEditNode" in by["image_edit_reconstruct"]["message"] and by["direct_transform"]["available"]
    res = _create(api, pid, d, "jobs-ve-1", status=422)
    assert res["error"]["code"] == "engine_unavailable"
    runtime_svc._CACHE.clear()
    monkeypatch.undo()
    assert len(_create(api, pid, d, "jobs-ve-2")["job_ids"]) == 1
