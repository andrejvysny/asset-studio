"""Raster derivatives + frame/atlas handling, including hostile archives. Fixtures are generated in code."""
from __future__ import annotations

import io
import stat
import zipfile

import numpy as np
import pytest
from assetstudio_processing import atlas, raster
from PIL import Image


def _png(w: int, h: int, color: tuple[int, int, int, int] = (200, 50, 50, 255)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (w, h), color).save(out, "PNG")
    return out.getvalue()


def _disc(size: int = 64) -> tuple[np.ndarray, np.ndarray]:
    yy, xx = np.mgrid[:size, :size]
    mask = (((yy - size / 2) ** 2 + (xx - size / 2) ** 2) < (size / 4) ** 2).astype(np.uint8) * 255
    rgb = np.full((size, size, 3), 220, np.uint8)
    rgb[mask > 0] = (30, 90, 200)
    return rgb, mask


def test_cutout_trim_and_bottom_pivot() -> None:
    rgb, mask = _disc()
    rgba = raster.apply_mask(rgb, mask)
    assert (rgba[mask == 0][:, :3] == (30, 90, 200)).all()  # colour bled outward: no grey halo under filtering
    content, box = raster.trim(rgba, 128)
    assert content.shape[:2] == (box[3] - box[1], box[2] - box[0])
    canvas, pivot = raster.fit_canvas(content, 128, 0.1, "bottom_center")
    assert canvas.shape == (128, 128, 4)
    alpha_rows = np.nonzero(canvas[..., 3].max(axis=1) >= 128)[0]
    assert abs(int(alpha_rows.max()) + 1 - pivot[1]) <= 1 and pivot[1] == 128 - round(128 * 0.1)
    center, cpiv = raster.fit_canvas(content, 0, 0.0, "center")
    assert center.shape[:2] == content.shape[:2] and cpiv == [content.shape[1] // 2, content.shape[0] // 2]


def test_empty_mask_and_bad_padding_rejected() -> None:
    rgb, _ = _disc()
    with pytest.raises(raster.RasterError):
        raster.apply_mask(rgb, np.zeros((64, 64), np.uint8))
    with pytest.raises(raster.RasterError):
        raster.fit_canvas(np.zeros((4, 4, 4), np.uint8), 32, 0.5, "center")


def test_square_variants_exact_sizes() -> None:
    rgba = np.zeros((40, 80, 4), np.uint8)
    out = raster.square_variants(rgba, [64, 32, 16, 32])
    assert sorted(out) == [16, 32, 64] and all(a.shape == (s, s, 4) for s, a in out.items())


def test_seam_ratio_separates_tileable_from_gradient() -> None:
    x = np.linspace(0, 2 * np.pi, 64, endpoint=False)
    tile = (127 + 100 * np.sin(x)[None, :, None] * np.ones((64, 1, 3))).astype(np.uint8)
    grad = np.tile(np.linspace(0, 255, 64, dtype=np.uint8)[None, :, None], (64, 1, 3))
    assert raster.seam_stats(tile)["ratio"] < 2.0
    assert raster.seam_stats(grad)["ratio"] > 10.0
    assert raster.tile_preview(tile, 3, 96).shape == (96, 96, 3)


def test_frames_natural_order_and_uniform_size() -> None:
    names, frames = atlas.decode_frames([("f10.png", _png(8, 8)), ("f2.png", _png(8, 8)), ("f1.png", _png(8, 8))])
    assert names == ["f1.png", "f2.png", "f10.png"] and frames[0].shape == (8, 8, 4)
    with pytest.raises(atlas.FrameError, match="differ in size"):
        atlas.decode_frames([("a.png", _png(8, 8)), ("b.png", _png(9, 8))])
    jpeg = io.BytesIO()
    Image.new("RGB", (8, 8)).save(jpeg, "JPEG")
    with pytest.raises(atlas.FrameError, match="unsupported"):
        atlas.decode_frames([("a.jpg", jpeg.getvalue())])


def test_pack_grid_rects_and_pow2() -> None:
    frames = [np.full((10, 12, 4), i, np.uint8) for i in range(5)]
    img, rects, cols, rows = atlas.pack_grid(frames, padding=1)
    assert (cols, rows) == (3, 2) and rects[4] == [1 + 13, 1 + 11, 12, 10]
    x, y, w, h = rects[3]
    assert (img[y:y + h, x:x + w] == 3).all()
    img2, *_ = atlas.pack_grid(frames, padding=1, pow2=True)
    assert img2.shape[:2] == (32, 64)
    with pytest.raises(atlas.FrameError, match="limit"):
        atlas.pack_grid([np.zeros((5000, 5000, 4), np.uint8)] * 4)


def _zip(entries: list[tuple[str, bytes]], symlink: str | None = None) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    return out.getvalue()


def test_zip_reads_frames_and_skips_os_metadata() -> None:
    files = atlas.read_zip(_zip([("seq/f1.png", _png(4, 4)), ("__MACOSX/seq/._f1.png", b"x"), ("seq/", b"")]))
    assert [n for n, _ in files] == ["seq/f1.png"]


@pytest.mark.parametrize("entries,symlink,match", [
    ([("../evil.png", b"x")], None, "unsafe path"),
    ([("/abs.png", b"x")], None, "unsafe path"),
    ([("a\\b.png", b"x")], None, "unsafe path"),
    ([], "link.png", "symlink"),
])
def test_zip_hostile_entries_rejected(entries: list[tuple[str, bytes]], symlink: str | None, match: str) -> None:
    with pytest.raises(atlas.FrameError, match=match):
        atlas.read_zip(_zip(entries, symlink))


def test_zip_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(atlas, "MAX_FRAMES", 2)
    with pytest.raises(atlas.FrameError, match="entries"):
        atlas.read_zip(_zip([(f"{i}.png", b"x") for i in range(3)]))
    monkeypatch.setattr(atlas, "MAX_ARCHIVE_BYTES", 1000)
    with pytest.raises(atlas.FrameError, match="expands"):
        atlas.read_zip(_zip([("big.png", b"\0" * 5000)]))  # compresses tiny, expands past the cap
    with pytest.raises(atlas.FrameError, match="not a zip"):
        atlas.read_zip(b"PK\x03\x04garbage")
