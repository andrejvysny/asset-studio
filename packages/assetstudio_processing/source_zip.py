"""ZIP layer of the source-package validator: structural scan and bounded streaming extraction.

Extraction never trusts headers: it inflates the raw member bytes itself, counts real output and aborts as soon
as it exceeds the declared size. Symlinks are never created and every output path is confined to the staging dir.
"""
from __future__ import annotations

import hashlib
import re
import stat
import struct
import unicodedata
import zipfile
import zlib
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, BinaryIO

from assetstudio_core.delivery import SAFE_PATH_PATTERN

from .source_report import SourcePackageError

CHUNK = 1 << 20
RATIO_LIMIT = 200
RATIO_MIN_BYTES = 1 << 20
EOCD_SIG = b"PK\x05\x06"
LOCAL_SIG = b"PK\x03\x04"
_SAFE = re.compile(SAFE_PATH_PATTERN)
_DRIVE = re.compile(r"^[A-Za-z]:")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _unsafe(detail: str, message: str, path: str | None = None) -> SourcePackageError:
    return SourcePackageError("unsafe_package", detail, message, path)


def _limit(detail: str, message: str, path: str | None = None) -> SourcePackageError:
    return SourcePackageError("resource_limit", detail, message, path)


def check_eocd(fp: BinaryIO, size: int, max_files: int) -> int:
    """Validate the end-of-central-directory record before zipfile reads the (possibly huge) directory."""
    tail_len = min(size, 22 + 0xFFFF)
    fp.seek(size - tail_len)
    tail = fp.read(tail_len)
    pos = tail.rfind(EOCD_SIG)
    if pos < 0 or len(tail) - pos < 22:
        raise _unsafe("bad_zip", "not a ZIP archive: no end-of-central-directory record")
    disk, cd_disk, n_disk, n_total, cd_size, cd_off, clen = struct.unpack("<HHHHLLH", tail[pos + 4:pos + 22])
    if pos + 22 + clen != len(tail):
        raise _unsafe("trailing_data", "data after the end-of-central-directory record")
    if disk or cd_disk or n_disk != n_total:
        raise _unsafe("multi_disk", "multi-disk archives are not accepted")
    if n_total == 0xFFFF or cd_size == 0xFFFFFFFF or cd_off == 0xFFFFFFFF:
        raise _unsafe("zip64", "ZIP64 archives are not accepted")
    if n_total > max_files:
        raise _limit("too_many_files", f"{n_total} members exceeds the limit of {max_files}")
    return n_total


def validate_name(name: str, max_depth: int) -> None:
    if _CONTROL.search(name):
        raise _unsafe("control_character", "member name contains a control character", name)
    if "\\" in name:
        raise _unsafe("backslash_path", "member name contains a backslash", name)
    if name.startswith("/") or _DRIVE.match(name):
        raise _unsafe("absolute_path", f"member path {name!r} is absolute", name)
    if unicodedata.normalize("NFC", name) != name:
        raise _unsafe("non_nfc", "member name is not Unicode NFC", name)
    segments = name.split("/")
    if ".." in segments:
        raise _unsafe("path_traversal", f"member path {name!r} escapes the package root", name)
    if "." in segments or "" in segments:
        raise _unsafe("invalid_path", "member name has an empty or '.' segment", name)
    if len(segments) > max_depth:
        raise _limit("depth", f"member depth {len(segments)} exceeds {max_depth}", name)
    if len(name) > 255 or not _SAFE.fullmatch(name):
        raise _unsafe("invalid_path", "member name outside [A-Za-z0-9_.-] segments", name)


def _check_member(info: zipfile.ZipInfo, max_depth: int) -> str | None:
    # orig_filename: ZipInfo.filename is truncated at the first NUL byte.
    name = info.orig_filename
    if info.flag_bits & 0x1:
        raise _unsafe("encrypted", "encrypted member", name)
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise _unsafe("compression_method", f"compression method {info.compress_type} is not allowed", name)
    if name.endswith("/"):
        if info.file_size or info.compress_size > 2:
            raise _unsafe("directory_with_content", "directory entry carries content", name)
        return None
    validate_name(name, max_depth)
    fmt = stat.S_IFMT(info.external_attr >> 16)
    if fmt == stat.S_IFLNK:
        raise _unsafe("symlink", "symbolic link member", name)
    if fmt not in (0, stat.S_IFREG):
        raise _unsafe("special_file", "non-regular file member", name)
    if info.file_size > RATIO_MIN_BYTES and info.file_size / max(info.compress_size, 1) > RATIO_LIMIT:
        raise _limit("compression_ratio", f"compression ratio above {RATIO_LIMIT}", name)
    return name


def check_members(infos: list[zipfile.ZipInfo], limits: Mapping[str, Any]) -> dict[str, zipfile.ZipInfo]:
    if len(infos) > limits["source_max_files"]:
        raise _limit("too_many_files", f"{len(infos)} members exceeds the limit of {limits['source_max_files']}")
    folded: set[str] = set()
    members: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in infos:
        name = _check_member(info, limits["source_max_depth"])
        if name is None:
            continue
        key = name.casefold()
        if key in folded:
            raise _unsafe("casefold_collision", f"{name!r} collides with another member case-insensitively", name)
        folded.add(key)
        total += info.file_size
        if total > limits["source_expanded_max_bytes"]:
            raise _limit("expanded_size", "declared expanded size exceeds the limit")
        members[name] = info
    return members


def scan_archive(fp: BinaryIO, limits: Mapping[str, Any]) -> dict[str, zipfile.ZipInfo]:
    """Structural scan: returns regular-file members by name; raises SourcePackageError on the first violation."""
    fp.seek(0, 2)
    declared = check_eocd(fp, fp.tell(), limits["source_max_files"])
    try:
        infos = zipfile.ZipFile(fp).infolist()
    except (zipfile.BadZipFile, NotImplementedError, struct.error, ValueError, EOFError) as e:
        raise _unsafe("bad_zip", f"unreadable ZIP central directory: {e}") from e
    if len(infos) != declared:
        raise _unsafe("bad_zip", "central directory entry count disagrees with the end record")
    return check_members(infos, limits)


def safe_dest(staging: Path, name: str) -> Path:
    root = staging.resolve()
    dest = (root / name).resolve()
    if dest == root or not dest.is_relative_to(root):
        raise _unsafe("path_traversal", f"member path {name!r} escapes the staging directory", name)
    return dest


def _local_data_start(fp: BinaryIO, info: zipfile.ZipInfo, archive_size: int) -> int:
    fp.seek(info.header_offset)
    head = fp.read(30)
    if len(head) != 30 or head[:4] != LOCAL_SIG:
        raise _unsafe("bad_zip", "bad local file header", info.orig_filename)
    name_len, extra_len = struct.unpack("<HH", head[26:30])
    raw_name = fp.read(name_len)
    encoding = "utf-8" if info.flag_bits & 0x800 else "cp437"
    if raw_name.decode(encoding, "replace") != info.orig_filename:
        raise _unsafe("bad_zip", "local header name differs from the central directory", info.orig_filename)
    start = fp.tell() + extra_len
    if start + info.compress_size > archive_size:
        raise _unsafe("bad_zip", "member data runs past the end of the archive", info.orig_filename)
    fp.seek(start)
    return start


def _inflate(dec: Any, chunk: bytes) -> Iterator[bytes]:
    data = dec.decompress(chunk, CHUNK)
    while True:
        if data:
            yield data
        if len(data) < CHUNK:
            return
        data = dec.decompress(dec.unconsumed_tail, CHUNK)


def _pieces(fp: BinaryIO, info: zipfile.ZipInfo) -> Iterator[bytes]:
    name = info.orig_filename
    dec = zlib.decompressobj(-15) if info.compress_type == zipfile.ZIP_DEFLATED else None
    left = info.compress_size
    while left > 0:
        chunk = fp.read(min(CHUNK, left))
        if not chunk:
            raise _unsafe("bad_zip", "truncated member data", name)
        left -= len(chunk)
        yield from (_inflate(dec, chunk) if dec else iter((chunk,)))
    if dec is not None:
        tail = dec.flush(CHUNK)
        if tail:
            yield tail
        if not dec.eof or dec.unused_data:
            raise _unsafe("bad_zip", "malformed deflate stream", name)


def extract_member(fp: BinaryIO, info: zipfile.ZipInfo, staging: Path, archive_size: int) -> tuple[str, int]:
    """Stream one member into staging; returns (sha256 hex, real size). Real bytes are bounded by file_size."""
    name = info.orig_filename
    dest = safe_dest(staging, name)
    _local_data_start(fp, info, archive_size)
    digest, crc, total = hashlib.sha256(), 0, 0
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("xb") as out:
            for piece in _pieces(fp, info):
                total += len(piece)
                if total > info.file_size:
                    raise _limit("size_exceeds_declared", "member expands past its declared size", name)
                crc = zlib.crc32(piece, crc)
                digest.update(piece)
                out.write(piece)
    except FileExistsError as e:
        raise _unsafe("path_conflict", "member path conflicts with another member", name) from e
    except NotADirectoryError as e:
        raise _unsafe("path_conflict", "member path nests under a file member", name) from e
    if total != info.file_size or crc != info.CRC:
        raise _unsafe("bad_zip", "member size or CRC-32 disagrees with its header", name)
    return digest.hexdigest(), total
