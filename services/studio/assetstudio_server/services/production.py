"""Build and publication commands: exact approved inputs in, BuildRuns attached at creation, staged tasks out."""
from __future__ import annotations

from typing import Any

from assetstudio_core.domain import AssetManifest, BuildRun, JobItem
from assetstudio_core.ids import derived_id
from assetstudio_core.inheritance import name_parts
from assetstudio_core.naming import render_name, variant_letters
from assetstudio_core.recipes import RECIPES, legacy_variant, validate_parameters
from assetstudio_storage.project import manifest_key
from assetstudio_storage.publication import name_key
from pydantic import BaseModel, Field

from ..coordinator.builds.common import create_run, reusable_qa_mask
from ..coordinator.stages import STAGES, new_task
from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from ..taskstore import Busy
from . import commands
from .prompts import ItemRef, job_of
from .records import load_build, load_decision, load_item, load_job
from .runs import active_run_for, record_wave
from .runtime import build_readiness
from .taskview import busy, item_tasks


class BuildItem(ItemRef):
    approval_id: str


class BuildApproved(BaseModel):
    items: list[BuildItem] = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=100)


REEXPORT_KEYS = ("exporter", "texture_size", "triangles", "remesh")


class ReexportItem(ItemRef):
    build_run_id: str
    overrides: dict[str, Any] = Field(default={}, max_length=len(REEXPORT_KEYS))


class Reexport(BaseModel):
    items: list[ReexportItem] = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=100)


class PublishItem(ItemRef):
    build_run_id: str
    make_current: bool = True
    expected_current_version: str | None = None


class Publish(BaseModel):
    items: list[PublishItem] = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=100)


class RetryPreview(BaseModel):
    items: list[ItemRef] = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=100)


def _fail(results: list[dict[str, Any]], unit: ItemRef, jid: str | None, e: ApiError) -> None:
    results.append({"job_id": jid, "item_id": unit.item_id, "ok": False, "code": e.code, "message": e.message})


def _exporters(studio: Studio) -> dict[str, Any]:
    return (studio.worker3d.health().get("exporters") or {}) if studio.worker3d is not None else {}


def _plan_build(studio: Studio, ctx: ProjectContext, job_id: str | None, req: BuildApproved,
                run_id: str | None) -> dict[str, Any]:
    results, units, exporters = [], [], None
    if job_id is not None:  # a Job-level command on a recipe that cannot build is refused as a whole
        recipe = RECIPES[load_job(ctx.store, job_id)[0].recipe_id]
        gate = build_readiness(studio, recipe.id)
        if gate["state"] != "ready":
            raise ApiError(422, "build_unavailable", f"{recipe.label}: {gate['reason']}", {"state": gate["state"]})
    for b in req.items:
        jid = None
        try:
            jid = job_of(b, job_id)
            job, _ = load_job(ctx.store, jid)
            recipe = RECIPES[job.recipe_id]
            gate = build_readiness(studio, recipe.id)
            if gate["state"] != "ready":
                raise ApiError(422, "build_unavailable", f"{recipe.label}: {gate['reason']}", {"state": gate["state"]})
            item, _ = load_item(ctx.store, jid, b.item_id)
            snap = ctx.store.read_snapshot(item.snapshot_sha)
            if (variant := legacy_variant(snap)) is not None:
                raise ApiError(422, "legacy_recipe", f"recorded with {variant}; fork this Job to the current "
                                                     "recipe before building (the old snapshot stays unchanged)")
            if recipe.build == "model3d":
                exporters = _exporters(studio) if exporters is None else exporters
                if not exporters.get(snap["parameters"].get("exporter", "")):
                    raise ApiError(422, "exporter_unavailable", f"exporter {snap['parameters'].get('exporter')!r} "
                                                                "is not installed in the 3D worker")
            if item.revision != b.expected_item_revision:
                raise ApiError(409, "stale_item", f"{item.name} changed (revision {item.revision}); reload")
            if item.approval != b.approval_id or item.regen_requested:
                raise ApiError(409, "stale_approval", "the approval changed; reload")
            if item.accepted_build is not None:
                raise ApiError(409, "accepted", "a build is already accepted")
            if busy(item_tasks(studio, ctx.id, item), "build"):
                raise ApiError(409, "busy", "a build is already running")
        except ApiError as e:
            _fail(results, b, jid, e)
            continue
        units.append({"job_id": jid, "item_id": b.item_id, "approval_id": b.approval_id, "build": recipe.build,
                      "run_id": run_id or active_run_for(studio, ctx, jid)})
        results.append({"job_id": jid, "item_id": b.item_id, "ok": True})
    return {"units": units, "results": results}


def build_chain(studio: Studio, ctx: ProjectContext, run: BuildRun, unit: dict[str, Any],
                wave_id: str | None) -> list[Any]:
    """Stage tasks for one build run. Segmentation runs on GPU1 only when no reusable QA mask exists."""
    common = {"project_id": ctx.id, "job_id": unit["job_id"], "item_id": unit["item_id"], "input_key": run.id,
              "inputs": {"build_run_id": run.id}, "run_id": unit.get("run_id"), "wave_id": wave_id}
    item, _ = load_item(ctx.store, unit["job_id"], unit["item_id"])
    snap = ctx.store.read_snapshot(item.snapshot_sha)
    out: list[Any] = []

    def add(stage: str, **kw: Any) -> None:
        deps = [out[-1].id] if out else []
        out.append(new_task(studio, STAGES[stage], deps=deps, **common, **kw))
    needs_mask = run.build == "model3d" or run.build == "sprite" or (
        run.build == "icon" and snap["parameters"].get("background") == "transparent")
    if run.kind == "reexport":
        add("bake", resident=_bake_res(studio, run, snap))
        add("finalize")
        return out
    if needs_mask and "segment" not in run.checkpoints:
        bound = load_decision(ctx.store, unit["job_id"], run.inputs["approval_id"]).bound
        reusable = reusable_qa_mask(ctx, unit["job_id"], bound, ctx.store.artifact(bound["artifact_id"]))
        if reusable is None:
            add("segment")
        else:  # the QA mask of exactly these bytes is reused: no GPU work, no model load
            add("segment", lane="cpu", resident="cpu")
    if run.build == "model3d":
        if "sample" not in run.checkpoints:
            add("sample")
        add("bake", resident=_bake_res(studio, run, snap))
        add("finalize")
    else:
        add("derive")
    return out


def _bake_res(studio: Studio, run: BuildRun, snap: dict[str, Any]) -> str:
    from ..coordinator.stages import residency

    exporter = (run.inputs.get("overrides") or {}).get("exporter") or snap["parameters"].get("exporter", "clean")
    return residency(studio, "bake", exporter=exporter)


@commands.replayable("build_approved")
def _build_effects(studio: Studio, ctx: ProjectContext, plan: dict[str, Any], cid: str) -> dict[str, Any]:
    results = {(r["job_id"], r["item_id"]): r for r in plan["results"]}
    created: list[str] = []
    wave_id = plan.get("wave_id")
    for u in plan["units"]:
        item, _ = load_item(ctx.store, u["job_id"], u["item_id"])
        if item.approval != u["approval_id"]:
            results[(u["job_id"], u["item_id"])] = {**u, "ok": False, "code": "stale_approval",
                                                    "message": "the approval changed before the build started"}
            continue
        run = create_run(studio, ctx, u["job_id"], item, u["approval_id"], u["build"], cid,
                         reexport_from=u.get("reexport_from"), overrides=u.get("overrides"))
        try:
            created += studio.journal.tasks.create(build_chain(studio, ctx, run, u, wave_id), cid)
        except Busy as e:
            results[(u["job_id"], u["item_id"])] = {**u, "ok": False, "code": "busy", "message": str(e)}
            continue
        results[(u["job_id"], u["item_id"])] = {**results[(u["job_id"], u["item_id"])], "build_run_id": run.id}
    record_wave(studio, ctx, plan, cid, "build")
    studio.events.publish("tasks", project_id=ctx.id)
    return {"command_id": cid, "results": list(results.values()), "tasks": created,
            "operation": {"id": cid, "kind": "build", "tasks": created} if created else None}


def build_approved(studio: Studio, ctx: ProjectContext, job_id: str | None, req: BuildApproved,
                   run_id: str | None = None) -> dict[str, Any]:
    body = {"job_id": job_id, "run_id": run_id, **req.model_dump(mode="json")}

    def plan(cid: str) -> dict[str, Any]:
        return {**_plan_build(studio, ctx, job_id, req, run_id), "run_id": run_id,
                "wave_id": derived_id("wav", cid) if run_id else None}
    return commands.execute(studio, ctx, "build_approved", req.idempotency_key, body, plan)


def reexport(studio: Studio, ctx: ProjectContext, job_id: str | None, req: Reexport) -> dict[str, Any]:
    """New build runs from stored raw intermediates with changed export parameters; TRELLIS.2 is not resampled."""
    for it in req.items:
        if bad := [k for k in it.overrides if k not in REEXPORT_KEYS]:
            raise ApiError(422, "invalid_parameters", f"not re-exportable: {bad}; allowed {list(REEXPORT_KEYS)}")
    body = {"job_id": job_id, **req.model_dump(mode="json")}

    def plan(cid: str) -> dict[str, Any]:
        results, units = [], []
        for r in req.items:
            jid = None
            try:
                jid = job_of(r, job_id)
                job, _ = load_job(ctx.store, jid)
                recipe = RECIPES[job.recipe_id]
                if recipe.build != "model3d":
                    raise ApiError(422, "reexport_unsupported",
                                   f"{recipe.label} builds have no raw intermediate to re-export")
                if errors := validate_parameters(recipe, r.overrides):
                    raise ApiError(422, "invalid_parameters", "; ".join(f"{k}: {m}" for k, m in errors))
                gate = build_readiness(studio, recipe.id)
                if gate["state"] != "ready":
                    raise ApiError(422, "build_unavailable", f"{recipe.label}: {gate['reason']}")
                item, _ = load_item(ctx.store, jid, r.item_id)
                run, _ = load_build(ctx.store, jid, r.build_run_id)
                if item.revision != r.expected_item_revision:
                    raise ApiError(409, "stale_item", f"{item.name} changed; reload")
                if run.item_id != item.id or "raw" not in run.artifacts:
                    raise ApiError(409, "no_raw", "that build has no stored raw intermediate for this item")
                if item.approval != run.inputs.get("approval_id") or item.regen_requested:
                    raise ApiError(409, "stale_approval", "the approval changed since that build; build again")
                if busy(item_tasks(studio, ctx.id, item), "build"):
                    raise ApiError(409, "busy", "a build is already running")
            except ApiError as e:
                _fail(results, r, jid, e)
                continue
            units.append({"job_id": jid, "item_id": r.item_id, "approval_id": run.inputs["approval_id"],
                          "build": "model3d", "reexport_from": run.id, "overrides": r.overrides,
                          "run_id": active_run_for(studio, ctx, jid)})
            results.append({"job_id": jid, "item_id": r.item_id, "ok": True})
        return {"units": units, "results": results}
    return commands.execute(studio, ctx, "build_approved", req.idempotency_key, body, plan)


def retry_preview(studio: Studio, ctx: ProjectContext, job_id: str | None, req: RetryPreview) -> dict[str, Any]:
    """Preview-only retry of valid builds whose preview failed: never regenerates or rebakes."""
    body = {"job_id": job_id, **req.model_dump(mode="json")}

    def plan(cid: str) -> dict[str, Any]:
        units, results = [], []
        for r in req.items:
            jid = job_of(r, job_id)
            item, _ = load_item(ctx.store, jid, r.item_id)
            run = load_build(ctx.store, jid, item.current_build)[0] if item.current_build else None
            if run is None or run.result != "valid" or run.preview != "failed":
                results.append({"job_id": jid, "item_id": r.item_id, "ok": False, "code": "no_failed_preview",
                                "message": "only valid builds with a failed preview can retry it"})
                continue
            units.append({"job_id": jid, "item_id": r.item_id, "build_run_id": run.id})
            results.append({"job_id": jid, "item_id": r.item_id, "ok": True})
        return {"units": units, "results": results}
    return commands.execute(studio, ctx, "retry_preview", req.idempotency_key, body, plan)


@commands.replayable("retry_preview")
def _preview_effects(studio: Studio, ctx: ProjectContext, plan: dict[str, Any], cid: str) -> dict[str, Any]:
    tasks = [new_task(studio, STAGES["preview"], project_id=ctx.id, job_id=u["job_id"], item_id=u["item_id"],
                      input_key=f"{u['build_run_id']}|{cid}", inputs={"build_run_id": u["build_run_id"]})
             for u in plan["units"]]
    created = studio.journal.tasks.create(tasks, cid) if tasks else []
    return {"command_id": cid, "results": plan["results"], "tasks": created}


# --- publication ---------------------------------------------------------------------------------------------------
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
                        "expected_item_revision": item.revision, "published": item.published is not None, **target})
    return out


def publish(studio: Studio, ctx: ProjectContext, job_id: str | None, req: Publish,
            run_id: str | None = None) -> dict[str, Any]:
    body = {"job_id": job_id, "run_id": run_id, **req.model_dump(mode="json")}

    def plan(cid: str) -> dict[str, Any]:
        taken: set[str] = set()
        results, units = [], []
        for p in req.items:
            jid = None
            try:
                jid = job_of(p, job_id)
                item, _ = load_item(ctx.store, jid, p.item_id)
                if item.revision != p.expected_item_revision:
                    raise ApiError(409, "stale_item", "item changed; reload")
                if item.accepted_build != p.build_run_id:
                    raise ApiError(409, "not_accepted", "only an accepted build result can be published")
                run, _ = load_build(ctx.store, jid, p.build_run_id)
                if run.result != "valid":
                    raise ApiError(409, "invalid_build", "structural validation did not pass")
                target = publish_target(ctx, item, taken)
                if not target["new_asset"] and target["current_version_id"] != p.expected_current_version:
                    raise ApiError(409, "stale_pointer", "the target asset's current version changed; reload")
                taken.add(target["name_id"])
            except ApiError as e:
                _fail(results, p, jid, e)
                continue
            units.append({**p.model_dump(), "job_id": jid, **target,
                          "run_id": run_id or active_run_for(studio, ctx, jid)})
            results.append({"job_id": jid, "item_id": p.item_id, "ok": True})
        return {"units": units, "results": results, "run_id": run_id,
                "wave_id": derived_id("wav", cid) if run_id else None}
    return commands.execute(studio, ctx, "publish", req.idempotency_key, body, plan)


@commands.replayable("publish")
def _publish_effects(studio: Studio, ctx: ProjectContext, plan: dict[str, Any], cid: str) -> dict[str, Any]:
    tasks = [new_task(studio, STAGES["publish"], project_id=ctx.id, job_id=u["job_id"], item_id=u["item_id"],
                      input_key=f"{u['build_run_id']}|{cid}", run_id=u["run_id"], wave_id=plan.get("wave_id"),
                      inputs={k: u[k] for k in ("build_run_id", "asset_id", "name_id", "make_current",
                                                "expected_current_version")})
             for u in plan["units"]]
    results = {(r["job_id"], r["item_id"]): r for r in plan["results"]}
    created = []
    for t in tasks:
        try:
            created += studio.journal.tasks.create([t], cid)
        except Busy as e:
            results[(t.job_id, t.item_id)] = {"job_id": t.job_id, "item_id": t.item_id, "ok": False, "code": "busy",
                                              "message": str(e)}
    record_wave(studio, ctx, plan, cid, "publication")
    return {"command_id": cid, "results": list(results.values()), "tasks": created,
            "operation": {"id": cid, "kind": "publish", "tasks": created} if created else None}


