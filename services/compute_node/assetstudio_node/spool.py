"""Result spool: outputs are durable (fsync + sha256) before they are reported; deleted only after a receipt."""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from assetstudio_protocol.execution import FileRef, ResultManifest
from pydantic import ValidationError

MANIFEST = "manifest.json"
_BLOCK = 1024 * 1024


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Spool:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def dir_for(self, attempt_id: str) -> Path:
        return self.root / attempt_id

    def write_file(self, attempt_id: str, name: str, data: bytes | Path, mime: str) -> FileRef:
        d = self.dir_for(attempt_id)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{name}.tmp"
        h = hashlib.sha256()
        size = 0
        with tmp.open("wb") as out:
            if isinstance(data, bytes):
                blocks = iter([data])
            else:
                src = data.open("rb")
                blocks = iter(lambda: src.read(_BLOCK), b"")
            try:
                for block in blocks:
                    out.write(block)
                    h.update(block)
                    size += len(block)
            finally:
                if not isinstance(data, bytes):
                    src.close()
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, d / name)
        _fsync_dir(d)
        return FileRef(name=name, sha256=h.hexdigest(), size=size, mime=mime)

    def write_manifest(self, attempt_id: str, generation: int, files: list[FileRef],
                       meta: dict) -> ResultManifest:
        """Written and fsynced last: its presence means the spool is complete."""
        manifest = ResultManifest(schema="assetstudio.result.v1", attempt_id=attempt_id, generation=generation,
                                  files=files, meta=meta)
        d = self.dir_for(attempt_id)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{MANIFEST}.tmp"
        with tmp.open("wb") as f:
            f.write(manifest.model_dump_json().encode())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, d / MANIFEST)
        _fsync_dir(d)
        return manifest

    def manifest(self, attempt_id: str) -> ResultManifest | None:
        path = self.dir_for(attempt_id) / MANIFEST
        try:
            return ResultManifest.model_validate_json(path.read_bytes())
        except (OSError, ValidationError):
            return None

    def files(self, attempt_id: str) -> list[Path]:
        d = self.dir_for(attempt_id)
        manifest = self.manifest(attempt_id)
        if manifest is not None:
            return [d / f.name for f in manifest.files]
        if not d.is_dir():
            return []
        return sorted(p for p in d.iterdir() if p.is_file() and not p.name.startswith("."))

    def delete(self, attempt_id: str) -> None:
        shutil.rmtree(self.dir_for(attempt_id), ignore_errors=True)

    def unsynced_bytes(self) -> int:
        """Bytes in spool dirs without a manifest: outputs not yet complete, so not yet reportable."""
        total = 0
        for aid in self.attempt_ids():
            if self.manifest(aid) is None:
                total += sum(p.stat().st_size for p in self.dir_for(aid).iterdir() if p.is_file())
        return total

    def attempt_ids(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())
