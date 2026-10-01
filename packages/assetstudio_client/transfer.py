"""Resumable chunked upload of one file. Reads are streamed: at most one chunk is in memory."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from assetstudio_protocol.transfer import IngestReceipt, UploadCreate, chunk_count, chunk_range

if TYPE_CHECKING:
    from .runner import RunnerClient

_READ = 1024 * 1024


def hash_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while block := f.read(_READ):
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


def upload_file(client: RunnerClient, path: Path, *, attempt_id: str, generation: int, role: str,
                mime: str) -> IngestReceipt:
    digest, size = hash_file(path)
    created = client.create_upload(UploadCreate(attempt_id=attempt_id, generation=generation, sha256=digest,
                                                size=size, role=role, mime=mime))
    received = set(client.upload_status(created.upload_id).received)
    with path.open("rb") as f:
        for index in range(chunk_count(size, created.chunk_size)):
            if index in received:
                continue
            start, end = chunk_range(index, size, created.chunk_size)
            f.seek(start)
            data = f.read(end - start)
            if len(data) != end - start:
                raise OSError(f"{path} changed while uploading")
            client.put_chunk(created.upload_id, index, data)
    return client.finalize_upload(created.upload_id)
