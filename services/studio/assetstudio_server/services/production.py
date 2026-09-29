"""Build and publication commands: exact approved inputs in, BuildRuns attached at creation, staged tasks out."""
from __future__ import annotations

from typing import Any, Literal

from assetstudio_core.canonical import now_iso
from assetstudio_core.domain import BuildRun, Job, JobItem, ReviewDecision
from assetstudio_core.ids import derived_id
from assetstudio_core.kinds import Kind
from assetstudio_core.recipes import RECIPES, legacy_variant, validate_parameters
from assetstudio_storage.repo import IntegrityError, NotFound
from pydantic import BaseModel, Field

from ..coordinator.builds.common import create_run, reusable_qa_mask
from ..coordinator.stages import STAGES, new_task
from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from ..taskstore import Busy
from . import build_modes, commands
from .prompts import ItemRef, job_of
from .publish_view import publish_preview as publish_preview  # noqa: PLC0414 (re-export for routers)
from .publish_view import publish_target
from .records import decision_key, load_build, load_decision, load_item, load_job, mutate_item
from .runs import active_run_for, record_wave
from .runtime import build_readiness
from .taskview import busy, item_tasks


class BuildItem(ItemRef):
    approval_id: str
    mode: Literal["build", "retry", "resample", "rebuild"] = "build"
    overrides: dict[str, Any] = Field(default={}, max_length=len(build_modes.REBUILD_KEYS))


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


class RunTransform(BaseModel):
    items: list[ItemRef] = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=100)


class RetryPreview(BaseModel):
    items: list[ItemRef] = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=100)


def refuse_direct(ctx: ProjectContext, job_id: str) -> None:
    """Direct-transform Jobs have no candidates or approvals: their only build path is run_transform."""
    if load_job(ctx.store, job_id)[0].direct:
        raise ApiError(422, "not_applicable", "direct transforms have no candidates; use run-transform")


def _fail(results: list[dict[str, Any]], unit: ItemRef, jid: str | None, e: ApiError) -> None:
    results.append({"job_id": jid, "item_id": unit.item_id, "ok": False, "code": e.code, "message": e.message})


def _exporters(studio: Studio) -> dict[str, Any]:
    return (studio.worker3d.health().get("exporters") or {}) if studio.worker3d is not None else {}


def _plan_build(studio: Studio, ctx: ProjectContext, job_id: str | None, req: BuildApproved,
                run_id: str | None) -> dict[str, Any]:
    results, units, exporters = [], [], None
    if job_id is not None:  # a Job-level command on a recipe that cannot build is refused as a whole
        refuse_direct(ctx, job_id)
        recipe = RECIPES[load_job(ctx.store, job_id)[0].recipe_id]
        gate = build_readiness(studio, recipe.id)
        if gate["state"] != "ready":
            raise ApiError(422, "build_unavailable", f"{recipe.label}: {gate['reason']}", {"state": gate["state"]})
        for b in req.items:
            build_modes.check(recipe, b.mode, b.overrides)
    for b in req.items:
        jid = None
        try:
            jid = job_of(b, job_id)
            refuse_direct(ctx, jid)
            job, _ = load_job(ctx.store, jid)
            recipe = RECIPES[job.recipe_id]
            gate = build_readiness(studio, recipe.id)
            if gate["state"] != "ready":
                raise ApiError(422, "build_unavailable", f"{recipe.label}: {gate['reason']}", {"state": gate["state"]})
            build_modes.check(recipe, b.mode, b.overrides)
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
                      "mode": b.mode, "overrides": b.overrides,
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
    if run.build.startswith("direct_"):  # deterministic CPU transform: no mask, segmentation or model stage
        add("derive")
        return out
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
                         reexport_from=u.get("reexport_from"), overrides=u.get("overrides"),
                         mode=u.get("mode", "build"))
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


# --- direct transforms (variants) ------------------------------------------------------------------------------------
def _direct_row(ctx: ProjectContext, job: Job) -> tuple[Any, Any]:
    from .variant_jobs import load_plan

    v = job.variant or {}
    plan = load_plan(ctx, v["plan_id"])
    row = next((r for r in plan.rows if r.id == v["row_id"]), None)
    if plan.sha256 != v["plan_sha256"] or row is None:
        raise ApiError(409, "stale_variant_plan", "the frozen variant plan does not match this Job")
    return plan, row


def _verified_source(ctx: ProjectContext, plan: Any) -> Any:
    ref = plan.source.artifact(plan.source.primary_role)
    if ref is None:
        raise ApiError(409, "source_integrity_failed", f"source has no {plan.source.primary_role} artifact")
    try:
        art = ctx.store.verify_artifact(ref.artifact_id, use_cache=False)
        if art.sha256 != ref.sha256:
            raise IntegrityError("artifact hash differs from the plan")
    except (IntegrityError, NotFound) as e:
        raise ApiError(409, "source_integrity_failed", f"the source failed verification: {e}") from e
    return ref


def _transform_unit(studio: Studio, ctx: ProjectContext, jid: str, r: ItemRef, cid: str) -> dict[str, Any]:
    job, _ = load_job(ctx.store, jid)
    if not job.direct or job.variant is None:
        raise ApiError(422, "not_direct", "only direct-transform Jobs run a transform")
    item, _ = load_item(ctx.store, jid, r.item_id)
    if item.revision != r.expected_item_revision:
        raise ApiError(409, "stale_item", f"{item.name} changed (revision {item.revision}); reload")
    if item.accepted_build is not None:
        raise ApiError(409, "accepted", "a result is already accepted")
    if busy(item_tasks(studio, ctx.id, item), "build"):
        raise ApiError(409, "busy", "a build is already running")
    if item.current_build and load_build(ctx.store, jid, item.current_build)[0].status not in (
            "failed", "blocked", "cancelled"):
        raise ApiError(409, "already_built", "this transform already has a result; review or accept it")
    plan, row = _direct_row(ctx, job)
    ref = _verified_source(ctx, plan)
    transform = row.glb_transform or row.raster_transform
    if transform is None:
        raise ApiError(422, "not_direct", "the variant row has no transform")
    bound = {"direct": True, "artifact_id": ref.artifact_id, "image_sha256": ref.sha256, "plan_id": plan.id,
             "plan_sha256": plan.sha256, "row_id": row.id, "transform": transform.model_dump(mode="json"),
             "confirm_duplicate": row.confirm_duplicate,
             "source": {"asset_id": plan.source.asset_id, "version_id": plan.source.version_id}}
    return {"job_id": jid, "item_id": item.id, "approval_id": derived_id("dec", cid, item.id), "bound": bound,
            "build": "direct_glb" if plan.output_kind is Kind.model3d else "direct_raster",
            "run_id": active_run_for(studio, ctx, jid)}


def run_transform(studio: Studio, ctx: ProjectContext, job_id: str | None, req: RunTransform) -> dict[str, Any]:
    """The human confirmation gate of a direct Job: binds the exact source bytes + transform, then builds."""
    body = {"job_id": job_id, **req.model_dump(mode="json")}

    def plan(cid: str) -> dict[str, Any]:
        if job_id is not None and not load_job(ctx.store, job_id)[0].direct:
            raise ApiError(422, "not_direct", "only direct-transform Jobs run a transform")
        results, units = [], []
        for r in req.items:
            jid = None
            try:
                jid = job_of(r, job_id)
                units.append(_transform_unit(studio, ctx, jid, r, cid))
            except ApiError as e:
                _fail(results, r, jid, e)
                continue
            results.append({"job_id": jid, "item_id": r.item_id, "ok": True})
        return {"units": units, "results": results, "run_id": None, "wave_id": None,
                "idempotency_key": req.idempotency_key}
    return commands.execute(studio, ctx, "run_transform", req.idempotency_key, body, plan)


def _confirm_transform(studio: Studio, ctx: ProjectContext, u: dict[str, Any], key: str) -> None:
    jid, did = u["job_id"], u["approval_id"]
    if ctx.store.repo.stat_object(decision_key(jid, did)) is None:
        ctx.store.create(decision_key(jid, did), ReviewDecision(
            id=did, gate="transform_confirmation", job_id=jid, item_id=u["item_id"], bound=u["bound"],
            decided_at=now_iso(), idempotency_key=key, run_id=u.get("run_id")))

    def apply(x: JobItem) -> None:
        if did not in x.decisions:
            x.decisions.append(did)
        x.approval, x.regen_requested = did, False
    mutate_item(studio, ctx, jid, u["item_id"], apply)


@commands.replayable("run_transform")
def _transform_effects(studio: Studio, ctx: ProjectContext, plan: dict[str, Any], cid: str) -> dict[str, Any]:
    results = {(r["job_id"], r["item_id"]): r for r in plan["results"]}
    created: list[str] = []
    for u in plan["units"]:
        key = (u["job_id"], u["item_id"])
        _confirm_transform(studio, ctx, u, plan["idempotency_key"])
        run = create_run(studio, ctx, u["job_id"], load_item(ctx.store, *key)[0], u["approval_id"], u["build"], cid)
        try:
            created += studio.journal.tasks.create(build_chain(studio, ctx, run, u, plan.get("wave_id")), cid)
        except Busy as e:
            results[key] = {**results[key], "ok": False, "code": "busy", "message": str(e)}
            continue
        results[key] = {**results[key], "build_run_id": run.id, "approval_id": u["approval_id"]}
    studio.events.publish("tasks", project_id=ctx.id)
    return {"command_id": cid, "results": list(results.values()), "tasks": created,
            "operation": {"id": cid, "kind": "build", "tasks": created} if created else None}


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
                if item.published is not None and item.published.build_run_id == p.build_run_id:
                    raise ApiError(409, "already_published", "this accepted result is already published as "
                                   f"{item.published.asset_id} v{item.published.display_version}")
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


