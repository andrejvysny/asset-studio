"""Library read models: actual assets (from the index) and planned requests (from the shot list), kept separate."""
from __future__ import annotations

from typing import Any

from assetstudio_core.canonical import pretty_json
from assetstudio_core.domain import AssetManifest, AssetVersion
from assetstudio_core.inheritance import descendants, resolve
from assetstudio_core.kinds import KINDS, Kind
from assetstudio_storage.project import manifest_key, version_key

from ..errors import ApiError
from ..registry import ProjectContext
from .shotlist import shot_statuses


def category_tree(ctx: ProjectContext) -> list[dict[str, Any]]:
    cfg, _ = ctx.config()
    counts = ctx.index.counts_by_category()
    by_parent: dict[str | None, list] = {}
    for c in cfg.categories:
        if not c.archived:
            by_parent.setdefault(c.parent_id, []).append(c)
    out: list[dict[str, Any]] = []

    def walk(parent: str | None, depth: int, path: str) -> None:
        for c in sorted(by_parent.get(parent, []), key=lambda c: c.label.lower()):
            p = f"{path}/{c.slug}" if path else c.slug
            kind = resolve(cfg, c.id)["kind"]
            out.append({"id": c.id, "label": c.label, "slug": c.slug, "path": p, "parent_id": c.parent_id,
                        "depth": depth, "kind": kind.value, "kind_source": kind.source,
                        "count": sum(counts.get(d, 0) for d in descendants(cfg, c.id))})
            walk(c.id, depth + 1, p)
    walk(None, 0, "")
    return out


def list_assets(ctx: ProjectContext, *, category_id: str | None, kind: str | None, origin: str | None,
                q: str | None, show_planned: bool, limit: int, offset: int) -> dict[str, Any]:
    cfg, _ = ctx.config()
    cats = descendants(cfg, category_id) if category_id else None
    if category_id and cfg.category(category_id) is None:
        raise ApiError(404, "unknown_category", f"category {category_id} does not exist")
    rows, total = ctx.index.query(categories=cats, kind=kind, origin=origin, q=q, limit=limit, offset=offset)
    planned: list[dict[str, Any]] = []
    if show_planned and origin in (None, "generated"):
        needle = (q or "").lower()
        for s in shot_statuses(ctx):
            if s["status"] not in ("planned", "in_batch"):
                continue
            if cats is not None and s["category_id"] not in cats:
                continue
            if kind and s["effective_kind"] != kind:
                continue
            if needle and needle not in s["name"].lower():
                continue
            planned.append(s)
    return {"items": [{**r, "kind_label": KINDS[Kind(r["kind"])].label} for r in rows], "total": total,
            "planned": planned, "planned_total": len(planned), "all_assets_total": ctx.index.count()}


def asset_detail(ctx: ProjectContext, asset_id: str, version_id: str | None) -> dict[str, Any]:
    manifest, _ = ctx.store.get(manifest_key(asset_id), AssetManifest)
    shown_id = version_id or manifest.current_version_id
    if shown_id is None or manifest.version(shown_id) is None:
        raise ApiError(404, "unknown_version", f"version {shown_id} not found for {asset_id}")
    version, _ = ctx.store.get(version_key(asset_id, shown_id), AssetVersion)
    cfg, _ = ctx.config()
    facts = []
    if manifest.category_id and cfg.category(manifest.category_id):
        for k, r in resolve(cfg, manifest.category_id).items():
            facts.append({"key": k, "value": r.value, "mode": r.mode.value, "source": r.source})
    cat = cfg.category(manifest.category_id) if manifest.category_id else None
    return {
        "manifest": manifest.model_dump(mode="json"),
        "manifest_json": pretty_json(manifest.model_dump(mode="json")).decode(),
        "kind_label": KINDS[manifest.kind].label,
        "category_label": cat.label if cat else None,
        "shown_version": version.model_dump(mode="json"),
        "is_current": shown_id == manifest.current_version_id,
        "facts": facts,
        "files": [{"role": role, **ref} for role, ref in version.artifacts.items()],
    }
