"""Shot list: requested assets. Content lives in shotlist.yaml; status is derived from batches + publications."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from assetstudio_core.domain import ShotItem, ShotList
from assetstudio_core.ids import is_id, new_id
from assetstudio_core.inheritance import resolve
from assetstudio_core.kinds import Kind
from assetstudio_core.shotlist_io import parse, validate_row
from pydantic import BaseModel, Field

from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from .records import load_items, load_job


class ShotInput(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=200)
    category_id: str | None = None
    kind: Kind | None = None
    brief: str = Field(default="", max_length=4000)
    priority: Literal["low", "med", "high"] = "med"
    notes: str = Field(default="", max_length=4000)
    target_asset_id: str | None = None
    external_id: str | None = None
    archived: bool = False


class SaveShotList(BaseModel):
    expected_revision: int
    items: list[ShotInput] = Field(max_length=5000)


class CommitImport(BaseModel):
    preview_id: str
    expected_revision: int
    actions: dict[str, Literal["create", "update", "skip"]] = {}  # row line -> action (overrides defaults)


def _effective_kind(ctx_cfg: Any, s: ShotItem) -> str | None:
    if s.kind is not None:
        return s.kind.value
    if s.category_id and ctx_cfg.category(s.category_id):
        k = resolve(ctx_cfg, s.category_id)["kind"].value
        return k
    return None


def shot_statuses(ctx: ProjectContext) -> list[dict[str, Any]]:
    from .jobs import job_ids

    shots, _ = ctx.store.read_shotlist()
    cfg, _ = ctx.config()
    membership: dict[str, dict[str, Any]] = {}
    for jid in job_ids(ctx):
        job, _ = load_job(ctx.store, jid)
        for item in load_items(ctx.store, job):
            if not item.shot_id or item.cancelled:
                continue
            rec = {"job_id": job.id, "job_alias": job.alias, "batch_id": job.id, "batch_alias": job.alias,
                   "item_id": item.id,
                   "published": item.published.model_dump() if item.published else None}
            prev = membership.get(item.shot_id)
            if prev is None or (rec["published"] and not prev["published"]):
                membership[item.shot_id] = rec
    out = []
    for s in shots.items:
        m = membership.get(s.id)
        status = "archived" if s.archived else "published" if m and m["published"] else \
            "in_batch" if m else "planned"
        out.append({**s.model_dump(mode="json"), "status": status, "membership": m,
                    "effective_kind": _effective_kind(cfg, s)})
    return out


def get_shotlist(ctx: ProjectContext) -> dict[str, Any]:
    shots, _ = ctx.store.read_shotlist()
    return {"revision": shots.revision, "items": shot_statuses(ctx)}


def _validate(ctx: ProjectContext, items: list[ShotItem]) -> None:
    cfg, _ = ctx.config()
    errors = []
    seen: set[str] = set()
    for i, s in enumerate(items):
        if s.id in seen:
            errors.append({"row": i, "message": f"duplicate id {s.id}"})
        seen.add(s.id)
        if s.category_id is not None and cfg.category(s.category_id) is None:
            errors.append({"row": i, "message": f"unknown category {s.category_id!r}"})
        if _effective_kind(cfg, s) is None:
            errors.append({"row": i, "message": f"{s.name}: kind required (category has no default kind)"})
    if errors:
        raise ApiError(422, "invalid_shotlist", "shot list has invalid rows", errors)


def save_shotlist(studio: Studio, ctx: ProjectContext, req: SaveShotList) -> dict[str, Any]:
    ctx.require_writable()
    with ctx.store.lock:
        shots, token = ctx.store.read_shotlist()
        if shots.revision != req.expected_revision:
            raise ApiError(409, "stale_shotlist", f"shot list changed (revision {shots.revision}); reload")
        items = []
        for s in req.items:
            if s.id is not None and not is_id(s.id, "shot"):
                raise ApiError(400, "invalid_id", f"invalid shot id {s.id}")
            prior = next((x for x in shots.items if x.id == s.id), None)
            items.append(ShotItem(**{**s.model_dump(), "id": s.id or new_id("shot"),
                                     "source": prior.source if prior else {"type": "manual"}}))
        _validate(ctx, items)
        ctx.store.write_shotlist(ShotList(revision=shots.revision + 1, items=items), token)
    studio.events.publish("shotlist", project_id=ctx.id)
    return get_shotlist(ctx)


def preview_import(studio: Studio, ctx: ProjectContext, filename: str, content: bytes) -> dict[str, Any]:
    cfg, _ = ctx.config()
    res = parse(filename, content)
    cat_ids = {c.id for c in cfg.categories}
    cat_kinds = {c.id: resolve(cfg, c.id)["kind"].value for c in cfg.categories}
    shots, _ = ctx.store.read_shotlist()
    by_ext = {s.external_id: s for s in shots.items if s.external_id}
    by_name = {s.name.lower(): s for s in shots.items}
    rows = []
    for r in res.rows:
        validate_row(r, cat_ids, cat_kinds)
        ext = r.values.get("id") or None
        match = by_ext.get(ext) if ext else None
        name_match = None if ext else by_name.get(r.values.get("name", "").lower())
        default = "skip" if r.errors else "update" if match else "create"
        rows.append({"line": r.line, "values": r.values, "errors": r.errors, "default_action": default,
                     "matches_id": match.id if match else None,
                     "possible_duplicate_of": name_match.id if name_match else None})
    digest = hashlib.sha256(content).hexdigest()
    preview_id = f"shotimp-{digest[:24]}"
    path = studio.settings.instance_dir / "staging" / "shotlist" / f"{preview_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"preview_id": preview_id, "filename": filename, "format": res.format, "columns": res.columns,
            "mapping": res.mapping, "errors": res.errors, "rows": rows, "revision": shots.revision}
    path.write_text(json.dumps(body))
    return body


def commit_import(studio: Studio, ctx: ProjectContext, req: CommitImport) -> dict[str, Any]:
    ctx.require_writable()
    path = studio.settings.instance_dir / "staging" / "shotlist" / f"{req.preview_id}.json"
    if not req.preview_id.startswith("shotimp-") or not path.is_file():
        raise ApiError(404, "unknown_preview", "import preview expired; upload the file again")
    preview = json.loads(path.read_text())
    if preview["errors"]:
        raise ApiError(422, "invalid_file", "the file has structural errors", preview["errors"])
    with ctx.store.lock:
        shots, token = ctx.store.read_shotlist()
        if shots.revision != req.expected_revision:
            raise ApiError(409, "stale_shotlist", "shot list changed since the preview; preview again")
        items = list(shots.items)
        created = updated = skipped = 0
        for row in preview["rows"]:
            action = req.actions.get(str(row["line"]), row["default_action"])
            if row["errors"] and action != "skip":
                raise ApiError(422, "row_has_errors", f"line {row['line']} has errors: {row['errors']}")
            v = row["values"]
            fields = {"name": v["name"], "category_id": v.get("category") or None, "kind": v.get("kind") or None,
                      "brief": v.get("brief", ""), "priority": v.get("priority", "med"), "notes": v.get("notes", ""),
                      "target_asset_id": v.get("target_asset_id") or None, "external_id": v.get("id") or None,
                      "source": {"type": "import", "file": preview["filename"], "line": row["line"]}}
            target = row["matches_id"] or row["possible_duplicate_of"]
            if action == "update" and target:
                items = [ShotItem(**{**x.model_dump(), **fields}) if x.id == target else x for x in items]
                updated += 1
            elif action == "create":
                items.append(ShotItem(id=new_id("shot"), **fields))
                created += 1
            else:
                skipped += 1
        _validate(ctx, items)
        ctx.store.write_shotlist(ShotList(revision=shots.revision + 1, items=items), token)
    path.unlink(missing_ok=True)
    studio.events.publish("shotlist", project_id=ctx.id)
    return {"created": created, "updated": updated, "skipped": skipped, **get_shotlist(ctx)}
