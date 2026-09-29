"""Schema + resource validation of the raw TRELLIS.2 intermediate (.npz), numpy only, BEFORE any GPU allocation.

Single source: the Studio validates at ingestion and worker3d (Python 3.10 image) copies this file verbatim.
`allow_pickle=False` alone does not bound memory or check the mesh semantics; this module does both.

Format "assetstudio.trellis2-raw/1": vertices (N, 3) f32 · faces (M, 3) i32 · attrs (L, C) f32 · coords (L, 3) i32 ·
voxel_size () f32 · layout JSON {channel: [start, stop]} over attrs · format () str.
"""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from typing import Any

import numpy as np

FORMAT = "assetstudio.trellis2-raw/1"
MEMBERS = {"format", "vertices", "faces", "attrs", "coords", "voxel_size", "layout"}
REQUIRED_CHANNELS = ("base_color", "metallic", "roughness", "alpha")  # what the GLB exporters bake
CHANNEL_WIDTH = {"base_color": 3, "metallic": 1, "roughness": 1, "alpha": 1}


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 1 << 30  # compressed request size
    max_decoded_bytes: int = 6 << 30  # sum of all decoded arrays
    max_vertices: int = 50_000_000
    max_faces: int = 100_000_000
    max_voxels: int = 50_000_000
    max_channels: int = 64
    max_grid: int = 4096


class RawInvalid(ValueError):
    """The intermediate is malformed or exceeds limits. Typed, so callers never retry it as transient."""


_SPEC: dict[str, tuple[str, int | None]] = {  # member -> (dtype kind+size, rank or None for scalar)
    "vertices": ("<f4", 2), "faces": ("<i4", 2), "attrs": ("<f4", 2), "coords": ("<i4", 2), "voxel_size": ("<f4", 0),
}


def _header(f: Any) -> tuple[tuple[int, ...], np.dtype]:
    version = np.lib.format.read_magic(f)
    if version == (1, 0):
        shape, fortran, dtype = np.lib.format.read_array_header_1_0(f)
    elif version == (2, 0):
        shape, fortran, dtype = np.lib.format.read_array_header_2_0(f)
    else:
        raise RawInvalid(f"unsupported .npy version {version}")
    if fortran:
        raise RawInvalid("fortran-ordered arrays are not accepted")
    if dtype.hasobject:
        raise RawInvalid("object arrays are not accepted")
    return shape, dtype


DEFAULT_LIMITS = Limits()


def load(data: bytes, limits: Limits = DEFAULT_LIMITS) -> dict[str, Any]:
    """Validate and decode on CPU. Returns numpy arrays + layout slices; raises RawInvalid on any problem."""
    if not data or len(data) > limits.max_bytes:
        raise RawInvalid("raw payload missing or larger than the limit")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise RawInvalid(f"not an npz archive: {e}") from e
    with zf:
        names = [i.filename for i in zf.infolist()]
        if len(names) != len(set(names)):
            raise RawInvalid("duplicate archive members")
        stems = {n[:-4] for n in names if n.endswith(".npy")}
        if len(stems) != len(names) or stems != MEMBERS:
            raise RawInvalid(f"members must be exactly {sorted(MEMBERS)}; got {sorted(names)}")
        if sum(i.file_size for i in zf.infolist()) > limits.max_decoded_bytes:
            raise RawInvalid("decoded size exceeds the limit")
        shapes: dict[str, tuple[int, ...]] = {}
        for name, (dt, rank) in _SPEC.items():
            with zf.open(f"{name}.npy") as f:
                shape, dtype = _header(f)
            if dtype.str != dt or len(shape) != rank:
                raise RawInvalid(f"{name}: expected {dt} rank {rank}, got {dtype.str} {shape}")
            shapes[name] = shape
        _check_shapes(shapes, limits)
        out: dict[str, Any] = {}
        for name in _SPEC:
            with zf.open(f"{name}.npy") as f:
                out[name] = np.lib.format.read_array(f, allow_pickle=False)
        fmt = _string(zf, "format")
        layout_text = _string(zf, "layout")
    if fmt != FORMAT:
        raise RawInvalid(f"unsupported raw format {fmt!r}")
    out["layout"] = _layout(layout_text, shapes["attrs"][1])
    _check_values(out, limits)
    out["voxel_size"] = float(out["voxel_size"])
    return out


def _string(zf: zipfile.ZipFile, name: str) -> str:
    with zf.open(f"{name}.npy") as f:
        shape, dtype = _header(f)
        if shape != () or dtype.kind != "U" or dtype.itemsize > 4 * 65536:
            raise RawInvalid(f"{name}: expected a short unicode scalar")
    with zf.open(f"{name}.npy") as f:
        return str(np.lib.format.read_array(f, allow_pickle=False))


def _check_shapes(s: dict[str, tuple[int, ...]], lim: Limits) -> None:
    (n, vw), (m, fw), (lv, c), (lc, cw) = s["vertices"], s["faces"], s["attrs"], s["coords"]
    if vw != 3 or fw != 3 or cw != 3:
        raise RawInvalid("vertices, faces and coords must have 3 columns")
    if not (0 < n <= lim.max_vertices and 0 < m <= lim.max_faces and 0 < lv <= lim.max_voxels):
        raise RawInvalid(f"empty or oversized arrays: {n} vertices, {m} faces, {lv} voxels")
    if lv != lc:
        raise RawInvalid(f"attrs ({lv}) and coords ({lc}) row counts differ")
    if not 0 < c <= lim.max_channels:
        raise RawInvalid(f"{c} attribute channels (limit {lim.max_channels})")


def _layout(text: str, channels: int) -> dict[str, slice]:
    try:
        raw = json.loads(text)
    except ValueError as e:
        raise RawInvalid(f"layout is not JSON: {e}") from e
    if not isinstance(raw, dict):
        raise RawInvalid("layout must be an object")
    out: dict[str, slice] = {}
    for k, v in raw.items():
        if (not isinstance(v, list) or len(v) != 2 or not all(isinstance(x, int) and not isinstance(x, bool)
                                                               for x in v) or not 0 <= v[0] < v[1] <= channels):
            raise RawInvalid(f"layout channel {k!r} has an invalid slice {v!r} for {channels} channels")
        out[k] = slice(v[0], v[1])
    for k in REQUIRED_CHANNELS:
        if k not in out:
            raise RawInvalid(f"layout lacks the required channel {k!r}")
        if out[k].stop - out[k].start != CHANNEL_WIDTH[k]:
            raise RawInvalid(f"channel {k!r} must be {CHANNEL_WIDTH[k]} wide")
    return out


def _check_values(a: dict[str, Any], lim: Limits) -> None:
    vs = float(a["voxel_size"])
    if not np.isfinite(vs) or vs <= 0 or 1.0 / vs > lim.max_grid:
        raise RawInvalid(f"voxel_size {vs} is not a finite positive size within a {lim.max_grid} grid")
    if not np.isfinite(a["vertices"]).all():
        raise RawInvalid("vertices contain NaN/inf")
    if not np.isfinite(a["attrs"]).all():
        raise RawInvalid("attrs contain NaN/inf")
    faces = a["faces"]
    if int(faces.min()) < 0 or int(faces.max()) >= a["vertices"].shape[0]:
        raise RawInvalid("face indices out of range")
    grid = int(round(1.0 / vs))
    coords = a["coords"]
    if int(coords.min()) < 0 or int(coords.max()) >= grid:
        raise RawInvalid(f"voxel coords outside the {grid}^3 grid")
