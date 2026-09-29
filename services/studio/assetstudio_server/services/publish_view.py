"""Read side of publication: where an accepted result will land, and the Publish tab rows."""
from __future__ import annotations

from typing import Any

from assetstudio_core.domain import AssetFamily, AssetManifest, Job, JobItem
from assetstudio_core.inheritance import name_parts
from assetstudio_core.naming import render_name, variant_letters
from assetstudio_storage.families import family_key
from assetstudio_storage.project import manifest_key
from assetstudio_storage.publication import name_key

from ..errors import ApiError
from ..registry import ProjectContext
from .records import load_item, load_job


def publish_target(ctx: ProjectContext, item: JobItem, taken: set[str]) -> dict[str, Any]:
    """Where an accepted result will land: a new version of the target asset, or a new asset with a free name
    (free by the authoritative name records, plus names already planned in this command)."""
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
        if name_id not in taken and ctx.store.repo.stat_object(name_key(name_id)) is None:
            return {"asset_id": None, "name_id": name_id, "new_asset": True, "current_version_id": None,
                    "next_display_version": 1}
    raise ApiError(409, "naming_exhausted", f"no free name for {item.name}")


def _variant_lineage(ctx: ProjectContext, job: Job) -> dict[str, Any]:
    """Family / Derived from rows of the Publish tab (variant Jobs only)."""
    v = job.variant
    if v is None:
        return {}
    fam = ctx.store.get_opt(family_key(v["family_id"]), AssetFamily)[0]
    return {"family": {"id": v["family_id"], "name": fam.name if fam else ""},
            "derived_from": {"asset_id": v["source_asset_id"], "version_id": v["source_version_id"],
                             "display_version": v["source_display_version"], "name": v["source_name"]}}


def publish_preview(ctx: ProjectContext, job_ids: list[str]) -> list[dict[str, Any]]:
    taken: set[str] = set()
    out = []
    for jid in job_ids:
        job, _ = load_job(ctx.store, jid)
        for iid in job.item_ids:
            item, _ = load_item(ctx.store, jid, iid)
            if item.accepted_build is None:
                continue
            target = publish_target(ctx, item, taken)
            taken.add(target["name_id"])
            out.append({"job_id": jid, "item_id": item.id, "name": item.name, "build_run_id": item.accepted_build,
                        "expected_item_revision": item.revision, "published": item.published is not None, **target,
                        **_variant_lineage(ctx, job)})
    return out
