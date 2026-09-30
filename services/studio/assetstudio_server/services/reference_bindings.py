"""Which reference images reach which consumer. Item references feed both the enhancer and compare-QA; a project
reference set (snapshot `reference_set`) feeds exactly the consumer its mode names. Selection is deterministic and
records what was left out, so a prompt or QA verdict never silently ignores an image."""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import Any, Literal

from assetstudio_core.domain import JobItem
from assetstudio_core.effects import ROUTING_VERSION
from assetstudio_storage.project import ProjectStore
from assetstudio_storage.repo import IntegrityError, NotFound

Usage = Literal["prompt_guidance", "qa_reference"]
ENHANCER_MAX_IMAGES = 4  # aux /enhance cap (prompt_service RefImage list max_length)
COMPARE_MAX_REFERENCES = 4


@dataclass(frozen=True)
class Binding:
    id: str
    origin: Literal["item", "project_set"]
    artifact_id: str
    sha256: str
    crop: dict[str, float] | None
    note: str


@dataclass(frozen=True)
class Selection:
    selected: tuple[Binding, ...]
    excluded: tuple[dict[str, str], ...]  # {"id", "reason"}

    def ids(self) -> list[str]:
        return [b.id for b in self.selected]

    def excluded_list(self) -> list[dict[str, str]]:
        return [dict(e) for e in self.excluded]


def _item_bindings(item: JobItem) -> list[Binding]:
    return [Binding(r["id"], "item", r["artifact_id"], r["sha256"], r.get("crop"), r.get("note", ""))
            for r in item.references]


def _set_images(store: ProjectStore, snap: dict[str, Any], usage: Usage) -> tuple[list[Binding], list[dict[str, str]]]:
    rs = snap.get("reference_set")
    if not rs or rs.get("mode") != usage:
        return [], []
    set_id = (snap.get("values") or {}).get("reference_set")
    images = rs.get("images") or []
    ids = [f"set:{set_id}:{i}" for i in range(len(images))]
    if snap.get("reference_routing") != ROUTING_VERSION:
        why = "project reference set not routed: Job created before project-set routing"
        return [], [{"id": i, "reason": why} for i in ids]
    out, missing = [], []
    for i, img in zip(ids, images, strict=True):
        try:  # studio.yaml does not pin artifacts: a missing one is reported, never a crash in QA planning
            sha = store.artifact(img["artifact_id"]).sha256
        except NotFound:
            missing.append({"id": i, "reason": f"image {img['artifact_id']} is missing from the project store"})
            continue
        out.append(Binding(i, "project_set", img["artifact_id"], sha, None, img.get("label") or img.get("role") or ""))
    return out, missing


def resolve_references(store: ProjectStore, item: JobItem, snap: dict[str, Any], usage: Usage,
                       limit: int) -> Selection:
    """Item references first (user intent), then the project set; the first `limit` are selected."""
    set_bindings, excluded = _set_images(store, snap, usage)
    candidates = _item_bindings(item) + set_bindings
    over = f"over the {limit}-image limit for {usage}"
    excluded += [{"id": b.id, "reason": over} for b in candidates[limit:]]
    return Selection(tuple(candidates[:limit]), tuple(excluded))


def _cropped(data: bytes, crop: dict[str, float]) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        w, h = im.size
        box = (round(crop["x"] * w), round(crop["y"] * h), round((crop["x"] + crop["w"]) * w),
               round((crop["y"] + crop["h"]) * h))
        out = io.BytesIO()
        im.crop(box).convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(out, "PNG")
    return out.getvalue()


def reference_images(store: ProjectStore, sel: Selection) -> list[tuple[bytes, str, str]]:
    """(bytes, "reference", note) per selected binding, verified against the recorded digest, crop applied.
    Raises IntegrityError when an image no longer matches what was attached."""
    out = []
    for b in sel.selected:
        data = store.artifact_bytes(b.artifact_id)
        if hashlib.sha256(data).hexdigest() != b.sha256:
            raise IntegrityError(f"reference {b.id} differs from what was attached")
        out.append((_cropped(data, b.crop) if b.crop else data, "reference", b.note))
    return out
