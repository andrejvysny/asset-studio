"""IM06 (H13) + IM07 (H14): worker3d raw validation and UV rasterizer. Needs torch: run in the worker image,
`make test-worker3d` (CPU tensors; pinned CUDA runtime is exercised by the GPU acceptance run).
Plain asserts + a runner, because the worker image ships without pytest."""
from __future__ import annotations

import io
import json
import sys
import traceback
import zipfile

import numpy as np
import rasterize
import raw_npz
import torch

LAYOUT = {"base_color": [0, 3], "metallic": [3, 4], "roughness": [4, 5], "alpha": [5, 6]}


def _npz(**over) -> bytes:
    arrays = dict(format=np.array(raw_npz.FORMAT), vertices=np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], np.float32) * .1,
                  faces=np.array([[0, 1, 2]], np.int32), attrs=np.zeros((2, 6), np.float32),
                  coords=np.array([[0, 0, 0], [1, 1, 1]], np.int32), voxel_size=np.array(1 / 64, np.float32),
                  layout=np.array(json.dumps(LAYOUT)))
    arrays.update(over)
    arrays = {k: v for k, v in arrays.items() if v is not None}
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrays)
    return buf.getvalue()


def _rejects(data: bytes, needle: str) -> None:
    try:
        raw_npz.load(data)
    except raw_npz.RawInvalid as e:
        assert needle in str(e), f"{needle!r} not in {e}"
        return
    raise AssertionError(f"accepted invalid raw ({needle})")


def test_valid_raw_loads_on_cpu() -> None:
    out = raw_npz.load(_npz())
    assert out["vertices"].shape == (3, 3) and out["layout"]["base_color"] == slice(0, 3)


def test_invalid_raws_rejected_before_cuda() -> None:
    _rejects(_npz(faces=np.array([[0, 1, -1]], np.int32)), "out of range")
    _rejects(_npz(faces=np.array([[0, 1, 3]], np.int32)), "out of range")
    _rejects(_npz(faces=np.array([[0.0, 1.0, 2.0]], np.float32)), "faces")
    _rejects(_npz(vertices=np.array([[0, 0, np.nan]] * 3, np.float32)), "NaN")
    _rejects(_npz(voxel_size=np.array(0.0, np.float32)), "voxel_size")
    _rejects(_npz(layout=np.array(json.dumps({"base_color": [0, 3]}))), "metallic")
    _rejects(_npz(layout=np.array(json.dumps({**LAYOUT, "base_color": [0, 9]}))), "invalid slice")
    _rejects(_npz(faces=np.zeros((0, 3), np.int32)), "empty")
    _rejects(_npz(coords=np.array([[0, 0, 0], [64, 0, 0]], np.int32)), "grid")
    _rejects(_npz(extra=np.zeros(1)), "members")
    _rejects(_npz(format=np.array("other/1")), "format")
    _rejects(b"not a zip", "npz")


def test_decoded_size_limit() -> None:
    try:
        raw_npz.load(_npz(attrs=np.zeros((2, 6), np.float32)), raw_npz.Limits(max_decoded_bytes=100))
    except raw_npz.RawInvalid as e:
        assert "decoded" in str(e)
        return
    raise AssertionError("limit not enforced")


def test_object_array_rejected() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k in raw_npz.MEMBERS:
            b = io.BytesIO()
            np.lib.format.write_array(b, np.array([object()], dtype=object) if k == "faces" else np.zeros(1),
                                      allow_pickle=True)
            z.writestr(f"{k}.npy", b.getvalue())
    _rejects(buf.getvalue(), "")


def _check_correlated(face_id: torch.Tensor, bary: torch.Tensor, uvs: torch.Tensor, faces: torch.Tensor,
                      size: int) -> None:
    """Every hit texel centre must be reproduced by its winning face's barycentric weights."""
    ys, xs = torch.nonzero(face_id >= 0, as_tuple=True)
    tri = uvs[faces[face_id[ys, xs]]] * size - 0.5
    p = (tri * bary[ys, xs].unsqueeze(-1)).sum(1)
    target = torch.stack([xs.float(), ys.float()], -1)
    assert torch.allclose(p, target, atol=1e-3), float((p - target).abs().max())


def test_shared_edges_and_overlaps_are_correlated_and_deterministic() -> None:
    g = torch.Generator().manual_seed(0)
    uvs = torch.rand(300, 2, generator=g)
    faces = torch.randint(0, 300, (400, 3), generator=g)
    faces[0] = torch.tensor([5, 5, 6])  # degenerate
    a1, b1 = rasterize.rasterize_uv_torch(uvs, faces, 64)
    a2, b2 = rasterize.rasterize_uv_torch(uvs, faces, 64)
    assert torch.equal(a1, a2) and torch.equal(b1, b2)
    assert not (a1 == 0).any()  # the degenerate face never wins
    _check_correlated(a1, b1, uvs, faces, 64)
    # winner = highest covering face index, independent of how faces are split into steps
    old = rasterize.CANDIDATE_BUDGET
    rasterize.CANDIDATE_BUDGET = 97
    try:
        a3, b3 = rasterize.rasterize_uv_torch(uvs, faces, 64)
    finally:
        rasterize.CANDIDATE_BUDGET = old
    assert torch.equal(a1, a3) and torch.allclose(b1, b3)


def test_one_huge_face_is_tiled_within_budget() -> None:
    uvs = torch.tensor([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
    faces = torch.tensor([[0, 1, 2], [1, 3, 2]])
    old = rasterize.CANDIDATE_BUDGET
    rasterize.CANDIDATE_BUDGET = 256
    seen: list[int] = []
    orig = rasterize._raster_step

    def spy(tri, lo, wh, offset, size, fid, bary):  # noqa: ANN001
        seen.append(int((wh[:, 0] * wh[:, 1]).sum()))
        return orig(tri, lo, wh, offset, size, fid, bary)
    rasterize._raster_step = spy
    try:
        f, b = rasterize.rasterize_uv_torch(uvs, faces, 128)
    finally:
        rasterize.CANDIDATE_BUDGET, rasterize._raster_step = old, orig
    assert max(seen) <= 256 and (f >= 0).float().mean() > 0.95
    _check_correlated(f, b, uvs, faces, 128)


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}\n{traceback.format_exc()}")
    sys.exit(1 if failed else 0)
