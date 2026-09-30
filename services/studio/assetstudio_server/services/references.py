"""Per-item reference images ("guidance only"): the text model describes them into the prompt and QA compares
candidates to them; they are never fed to the image model. Every change bumps `references_revision`, which prompt
confirmation binds, so a prompt written against older references cannot be confirmed silently."""
from __future__ import annotations

import hashlib
import io
import math
from typing import Any, Literal

from assetstudio_core.domain import Artifact, JobItem
from assetstudio_core.ids import derived_id
from assetstudio_storage.repo import IntegrityError, NotFound
from pydantic import BaseModel, Field, model_validator

from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from . import media as media_svc
from .records import load_item, load_job, mutate_item
from .taskview import busy, item_tasks

MAX_REFERENCES = 4
NOTE_MAX = 500
LIBRARY_ROLES = ("image", "preview")
Preset = Literal["conservative", "creative"]


class Crop(BaseModel):
    """Normalized rectangle of the image (0..1), origin top-left. Bounds are checked by `check_crop` (422)."""
    x: float
    y: float
    w: float
    h: float


def check_crop(crop: Crop | None) -> None:
    if crop is None:
        return
    ok = (all(math.isfinite(v) for v in (crop.x, crop.y, crop.w, crop.h)) and crop.x >= 0 and crop.y >= 0
          and crop.w > 0 and crop.h > 0 and crop.x + crop.w <= 1 + 1e-9 and crop.y + crop.h <= 1 + 1e-9)
    if not ok:
        raise ApiError(422, "invalid_crop", "crop is normalized 0..1 with w,h > 0, x+w <= 1 and y+h <= 1")


class LibraryRef(BaseModel):
    asset_id: str
    version_id: str
    role: Literal["image", "preview"] | None = None


class NewReference(BaseModel):
    """CreateJob form: an uploaded artifact (POST references:upload)."""
    artifact_id: str | None = None
    media_id: str | None = None
    note: str = Field(default="", max_length=NOTE_MAX)
    crop: Crop | None = None
    label: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def _one_source(self) -> NewReference:
        if (self.artifact_id is None) == (self.media_id is None):
            raise ValueError("give exactly one of artifact_id / media_id")
        return self


class AddReference(BaseModel):
    artifact_id: str | None = None
    library: LibraryRef | None = None
    media_id: str | None = None
    note: str = Field(default="", max_length=NOTE_MAX)
    crop: Crop | None = None
    label: str | None = Field(default=None, max_length=80)
    expected_item_revision: int

    @model_validator(mode="after")
    def _one_source(self) -> AddReference:
        if sum(v is not None for v in (self.artifact_id, self.library, self.media_id)) != 1:
            raise ValueError("give exactly one of artifact_id / library / media_id")
        return self


class UpdateReference(BaseModel):
    reference_id: str
    note: str | None = Field(default=None, max_length=NOTE_MAX)
    crop: Crop | None = None
    expected_item_revision: int


class RemoveReference(BaseModel):
    reference_id: str
    expected_item_revision: int


class SetPreset(BaseModel):
    preset: Preset
    expected_item_revision: int


def _image_artifact(ctx: ProjectContext, artifact_id: str) -> Artifact:
    try:
        art = ctx.store.verify_artifact(artifact_id)
    except NotFound as e:
        raise ApiError(422, "unknown_artifact", f"artifact {artifact_id} does not exist") from e
    except IntegrityError as e:
        raise ApiError(409, "artifact_corrupt", f"artifact {artifact_id} failed verification") from e
    if not art.mime.startswith("image/"):
        raise ApiError(422, "not_an_image", f"artifact {artifact_id} is not an image")
    return art


def _library_artifact(ctx: ProjectContext, lib: LibraryRef) -> Artifact:
    from .variants import resolve_source

    src = resolve_source(ctx, lib.asset_id, lib.version_id)
    roles = (lib.role,) if lib.role else LIBRARY_ROLES
    ref = next((a for r in roles if (a := src.artifact(r)) is not None), None)
    if ref is None:
        raise ApiError(422, "no_reference_image", "this version has no image or preview to use as a reference")
    return _image_artifact(ctx, ref.artifact_id)


def new_reference(ctx: ProjectContext, item_id: str, seq: int, *, artifact_id: str | None, library: LibraryRef | None,
                  note: str, crop: Crop | None, label: str | None, media_id: str | None = None) -> dict[str, Any]:
    check_crop(crop)
    if library:
        art, origin = _library_artifact(ctx, library), "library"
    elif media_id:
        art, origin = _image_artifact(ctx, media_svc.resolve_media_artifact(ctx, media_id)), "media"
    else:
        art, origin = _image_artifact(ctx, artifact_id or ""), "upload"
    ref = {"id": derived_id("jrf", item_id, str(seq)), "artifact_id": art.id, "sha256": art.sha256,
           "origin": origin, "note": note.strip(), "crop": crop.model_dump() if crop else None, "label": label,
           "library": library.model_dump() if library else None}
    if origin == "media":  # key only for media so existing record shapes stay byte-identical
        ref["media_id"] = media_id
    return ref


def resolve_new(ctx: ProjectContext, item_id: str, refs: list[NewReference]) -> list[dict[str, Any]]:
    """Job-creation references: revision 0 -> `len(refs)` seeds ids, so they are stable per item."""
    if len(refs) > MAX_REFERENCES:
        raise ApiError(422, "too_many_references", f"at most {MAX_REFERENCES} references per item")
    return [new_reference(ctx, item_id, n + 1, artifact_id=r.artifact_id, library=None, note=r.note, crop=r.crop,
                          label=r.label, media_id=r.media_id) for n, r in enumerate(refs)]


def _change(studio: Studio, ctx: ProjectContext, job_id: str, item_id: str, expected: int, fn: Any) -> dict[str, Any]:
    if load_job(ctx.store, job_id)[0].direct:
        raise ApiError(409, "direct_job", "direct transform Jobs have no prompt to guide")
    tasks = item_tasks(studio, ctx.id, load_item(ctx.store, job_id, item_id)[0])

    def apply(item: JobItem) -> None:
        if busy(tasks, "generate"):
            raise ApiError(409, "busy", "generation is running for this item")
        fn(item)
    item = mutate_item(studio, ctx, job_id, item_id, apply, expected)
    return {"item_id": item_id, "revision": item.revision, "references_revision": item.references_revision,
            "enhance_preset": item.enhance_preset, "references": item.references}


def add_reference(studio: Studio, ctx: ProjectContext, job_id: str, item_id: str, req: AddReference) -> dict[str, Any]:
    def fn(item: JobItem) -> None:
        if len(item.references) >= MAX_REFERENCES:
            raise ApiError(422, "too_many_references", f"at most {MAX_REFERENCES} references per item")
        ref = new_reference(ctx, item.id, item.references_revision + 1, artifact_id=req.artifact_id,
                            library=req.library, note=req.note, crop=req.crop, label=req.label,
                            media_id=req.media_id)
        item.references = [*item.references, ref]
        item.references_revision += 1
    return _change(studio, ctx, job_id, item_id, req.expected_item_revision, fn)


def update_reference(studio: Studio, ctx: ProjectContext, job_id: str, item_id: str,
                     req: UpdateReference) -> dict[str, Any]:
    sent = req.model_fields_set

    def fn(item: JobItem) -> None:
        ref = next((r for r in item.references if r["id"] == req.reference_id), None)
        if ref is None:
            raise ApiError(404, "unknown_reference", f"reference {req.reference_id} is not on this item")
        if "note" in sent and req.note is not None:
            ref["note"] = req.note.strip()
        if "crop" in sent:  # explicit null clears
            check_crop(req.crop)
            ref["crop"] = req.crop.model_dump() if req.crop else None
        item.references = [dict(r) for r in item.references]
        item.references_revision += 1
    return _change(studio, ctx, job_id, item_id, req.expected_item_revision, fn)


def remove_reference(studio: Studio, ctx: ProjectContext, job_id: str, item_id: str,
                     req: RemoveReference) -> dict[str, Any]:
    def fn(item: JobItem) -> None:
        if all(r["id"] != req.reference_id for r in item.references):
            raise ApiError(404, "unknown_reference", f"reference {req.reference_id} is not on this item")
        item.references = [r for r in item.references if r["id"] != req.reference_id]
        item.references_revision += 1
    return _change(studio, ctx, job_id, item_id, req.expected_item_revision, fn)


def set_preset(studio: Studio, ctx: ProjectContext, job_id: str, item_id: str, req: SetPreset) -> dict[str, Any]:
    def fn(item: JobItem) -> None:
        item.enhance_preset = req.preset
        item.references_revision += 1  # the preset shapes the instruction text: same staleness rule
    return _change(studio, ctx, job_id, item_id, req.expected_item_revision, fn)


def _cropped(data: bytes, crop: dict[str, float]) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        w, h = im.size
        box = (round(crop["x"] * w), round(crop["y"] * h), round((crop["x"] + crop["w"]) * w),
               round((crop["y"] + crop["h"]) * h))
        out = io.BytesIO()
        im.crop(box).convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(out, "PNG")
    return out.getvalue()


def reference_inputs(ctx: ProjectContext, item: JobItem) -> list[tuple[bytes, str, str]]:
    """(bytes, "reference", note) per item reference, verified against the recorded digest, crop applied.
    Raises IntegrityError when a reference no longer matches what the user attached."""
    out = []
    for r in item.references[:MAX_REFERENCES]:
        data = ctx.store.artifact_bytes(r["artifact_id"])
        if hashlib.sha256(data).hexdigest() != r["sha256"]:
            raise IntegrityError(f"reference {r['id']} differs from what was attached")
        out.append((_cropped(data, r["crop"]) if r.get("crop") else data, "reference", r.get("note", "")))
    return out
