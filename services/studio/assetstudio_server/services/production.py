"""Build and publication commands: explicit approved inputs in, durable operations out."""
from __future__ import annotations

from typing import Any

from assetstudio_core.domain import AssetManifest, BatchItem
from assetstudio_core.inheritance import name_parts
from assetstudio_core.naming import render_name, variant_letters
from assetstudio_core.recipes import RECIPES
from assetstudio_storage.project import manifest_key
from pydantic import BaseModel, Field

from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from .prompts import ItemRef, _outcome
from .records import load_batch, load_build, load_item, mutate_item, set_task


class BuildItem(ItemRef):
    approval_id: str


class BuildApproved(BaseModel):
    items: list[BuildItem] = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=100)


class PublishItem(ItemRef):
    build_run_id: str
    make_current: bool = True
    expected_current_version: str | None = None


class Publish(BaseModel):
    items: list[PublishItem] = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=100)


def build_approved(studio: Studio, ctx: ProjectContext, batch_id: str, req: BuildApproved) -> dict[str, Any]:
    ctx.require_writable()
    batch, _ = load_batch(ctx.store, batch_id)
    recipe = RECIPES[batch.recipe_id]
    if recipe.build is None:
        raise ApiError(422, "build_unavailable", f"{recipe.label}: {recipe.build_blocked_reason}")
    if (prior := studio.journal.command_result(req.idempotency_key, req.model_dump(mode="json"))) is not None:
        return prior
    results, eligible = [], []
    for b in req.items:
        def apply(item: BatchItem, b: BuildItem = b) -> None:
            if item.approval != b.approval_id or item.regen_requested:
                raise ApiError(409, "stale_approval", "the approval changed; reload")
            if item.accepted_build is not None:
                raise ApiError(409, "accepted", "a build is already accepted")
            if item.tasks.get("build") and item.tasks["build"].state in ("queued", "running"):
                raise ApiError(409, "busy", "a build is already running")
        out = _outcome(studio, ctx, batch_id, b.item_id, apply, b.expected_item_revision)
        results.append(out)
        if out["ok"]:
            eligible.append({"item_id": b.item_id, "approval_id": b.approval_id})
    op_public = None
    lane = "gpu1" if "birefnet" in recipe.build_models else "cpu"  # may need segmentation when QA had no mask
    if eligible:
        op, created = studio.journal.enqueue(project_id=ctx.id, batch_id=batch_id, kind="build", lane=lane,
                                             affinity=f"build.{recipe.build}",
                                             payload={"batch_id": batch_id, "items": eligible},
                                             idempotency_key=req.idempotency_key, hold=True)
        if created:
            for e in eligible:
                mutate_item(studio, ctx, batch_id, e["item_id"], lambda x: set_task(x, "build", op.id, "queued"))
            studio.journal.release(op.id)
        op_public = op.public()
    response = {"results": results, "operation": op_public}
    studio.journal.record_command(req.idempotency_key, req.model_dump(mode="json"), response)
    return response


def publish_target(ctx: ProjectContext, item: BatchItem, taken: set[str]) -> dict[str, Any]:
    """Where an accepted result will land: a new version of the target asset, or a new asset with a free name."""
    if item.target_asset_id:
        manifest, _ = ctx.store.get(manifest_key(item.target_asset_id), AssetManifest)
        return {"asset_id": manifest.asset_id, "name_id": manifest.name_id, "new_asset": False,
                "current_version_id": manifest.current_version_id,
                "next_display_version": 1 + max((v.display_version for v in manifest.versions), default=0)}
    snap = ctx.store.read_snapshot(item.snapshot_sha)
    cfg, _ = ctx.config()
    root, sub = name_parts(cfg, snap["category_id"])
    template = snap["values"].get("naming") or "{name}"
    for letter in variant_letters():
        name_id = render_name(template, item.name, root, sub, snap["recipe"]["kind"], letter)
        if "{variant}" not in template and "{v}" not in template and letter != "a":
            name_id = f"{name_id}_{letter}"
        if name_id not in taken:
            return {"asset_id": None, "name_id": name_id, "new_asset": True, "current_version_id": None,
                    "next_display_version": 1}
    raise ApiError(409, "naming_exhausted", f"no free name for {item.name}")


def publish_preview(ctx: ProjectContext, batch_id: str) -> list[dict[str, Any]]:
    batch, _ = load_batch(ctx.store, batch_id)
    taken = ctx.index.name_ids()
    out = []
    for iid in batch.item_ids:
        item, _ = load_item(ctx.store, batch_id, iid)
        if item.accepted_build is None:
            continue
        target = publish_target(ctx, item, taken)
        taken.add(target["name_id"])
        out.append({"item_id": item.id, "name": item.name, "build_run_id": item.accepted_build,
                    "expected_item_revision": item.revision, "published": item.published is not None, **target})
    return out


def publish(studio: Studio, ctx: ProjectContext, batch_id: str, req: Publish) -> dict[str, Any]:
    ctx.require_writable()
    if (prior := studio.journal.command_result(req.idempotency_key, req.model_dump(mode="json"))) is not None:
        return prior
    taken = ctx.index.name_ids()
    results, eligible = [], []
    for p in req.items:
        try:
            item, _ = load_item(ctx.store, batch_id, p.item_id)
            if item.revision != p.expected_item_revision:
                raise ApiError(409, "stale_item", "item changed; reload")
            if item.accepted_build != p.build_run_id:
                raise ApiError(409, "not_accepted", "only an accepted build result can be published")
            run, _ = load_build(ctx.store, batch_id, p.build_run_id)
            if run.result != "valid":
                raise ApiError(409, "invalid_build", "structural validation did not pass")
            target = publish_target(ctx, item, taken)
            if not target["new_asset"] and target["current_version_id"] != p.expected_current_version:
                raise ApiError(409, "stale_pointer", "the target asset's current version changed; reload")
            taken.add(target["name_id"])
        except ApiError as e:
            results.append({"item_id": p.item_id, "ok": False, "code": e.code, "message": e.message})
            continue
        eligible.append({**p.model_dump(), **target})
        results.append({"item_id": p.item_id, "ok": True})
    op_public = None
    if eligible:
        op, created = studio.journal.enqueue(project_id=ctx.id, batch_id=batch_id, kind="publish", lane="cpu",
                                             affinity="publish", payload={"batch_id": batch_id, "items": eligible},
                                             idempotency_key=req.idempotency_key, hold=True)
        if created:
            for e in eligible:
                mutate_item(studio, ctx, batch_id, e["item_id"], lambda x: set_task(x, "publish", op.id, "queued"))
            studio.journal.release(op.id)
        op_public = op.public()
    response = {"results": results, "operation": op_public}
    studio.journal.record_command(req.idempotency_key, req.model_dump(mode="json"), response)
    return response
