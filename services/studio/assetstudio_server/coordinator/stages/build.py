"""Build + publication stages. Builds read only the bound approval; publication commits exact accepted runs."""
from __future__ import annotations

import json
from typing import Any

from assetstudio_core.domain import AssetManifest, BuildRun, Job, JobItem, Published
from assetstudio_core.kinds import Kind, Origin
from assetstudio_core.variants import Derivation, VariantPlan
from assetstudio_processing.images import ImageRejected
from assetstudio_processing.raster import RasterError
from assetstudio_storage.families import get_family
from assetstudio_storage.project import manifest_key
from assetstudio_storage.publication import NewAsset, PublishRequest, StalePointer, publish
from assetstudio_storage.repo import Conflict, IntegrityError, NotFound, StorageError

from ...models import load_lock
from ...provenance import add_build_components, derived_licence, licence_summary
from ...services.records import (
    build_key,
    load_cset,
    load_decision,
    load_item,
    load_job,
    load_prompt,
    load_qa,
    mutate_item,
)
from ..builds import model3d as m3d
from ..builds.common import BuildInput, finish_run, foreground_mask, open_input, render_preview
from ..builds.kinds import BUILDS
from ..errors import ItemFailed
from ..runner import TaskEnv

# Kept on the build run (re-export, provenance) but not shipped as version files: raw TRELLIS output is ~100-300 MB.
INTERMEDIATE_ROLES = ("raw", "cutout", "mask", "model_unsized", "model_unmaterialized")


def segment(env: TaskEnv) -> dict[str, Any]:
    inp = open_input(env)
    if inp.run.build == "model3d":  # type: ignore[union-attr]
        return {"cutout": m3d.segment(inp)}
    if (cp := inp.done("segment")) is None:
        foreground_mask(inp, allow_compute=True)
        mask = inp.meta.get("mask", {})
        inp.checkpoint("segment", {"mask": mask["artifact_id"]}, inputs={"source_sha256": inp.source.sha256},
                       identities={"mask": mask})
        cp = inp.done("segment")
    return {"mask": cp.outputs["mask"]}  # type: ignore[union-attr]


def sample(env: TaskEnv) -> dict[str, Any]:
    return {"raw": m3d.sample(open_input(env))}


def bake(env: TaskEnv) -> dict[str, Any]:
    return {"model": m3d.bake(open_input(env))}


def finalize(env: TaskEnv) -> dict[str, Any]:
    inp = open_input(env)
    m3d.finalize(inp)
    return finish_run(inp, "model3d")


def derive(env: TaskEnv) -> dict[str, Any]:
    """CPU derivation of image kinds (canvas, variants, passthrough, material checks)."""
    inp = open_input(env)
    assert inp.run is not None
    fn = BUILDS.get(inp.run.build)
    if fn is None:
        raise ItemFailed(f"no build implementation {inp.run.build}", "unsupported_configuration")
    try:
        fn(inp)
    except (RasterError, ImageRejected) as e:
        inp.check("derive", False, str(e)[:300])
    return finish_run(inp, inp.run.build)


def preview(env: TaskEnv) -> dict[str, Any]:
    """Preview-only retry: never regenerates or rebakes; renders from the delivered model bytes."""
    inp = open_input(env)
    assert inp.run is not None
    model = inp.run.artifacts.get("model")
    image = inp.run.artifacts.get("image") if inp.run.build == "direct_raster" else None
    if model is None and image is None:
        raise ItemFailed("this run has no model to preview", "input_invalid")
    from assetstudio_processing.images import thumbnail_png
    from assetstudio_processing.render import preview_png

    data = env.ctx.store.artifact_bytes(model or image)  # type: ignore[arg-type]
    inp.preview = (lambda: preview_png(data)) if model else (lambda: thumbnail_png(data))
    inp.run.status = "succeeded"
    render_preview(inp)
    return {"preview": inp.run.preview}


def _models_of(env: TaskEnv, gen: dict[str, Any], used: list[str]) -> tuple[list[dict[str, Any]], str]:
    """Execution receipts when the generation recorded them; else today's lock, marked as a reconstruction."""
    if "model_receipts" in gen:
        return [{"key": r["key"], **{k: r[k] for k in ("repo", "revision", "files") if k in r},
                 **({"missing_from_lock": True} if r.get("missing_from_lock") else {})}
                for r in gen["model_receipts"]], "execution_receipt"
    lock = load_lock(env.studio.settings.config_dir)["models"]
    return [{"key": k, "repo": lock[k]["repo"], "revision": lock[k]["revision"], "files": lock[k]["files"],
             "reconstructed_from_current_lock": True} for k in used if k in lock], "reconstructed"


def _licence_of(env: TaskEnv, gen: dict[str, Any], used: list[str], run: BuildRun) -> dict[str, Any]:
    config_dir, components = env.studio.settings.config_dir, run.inputs.get("components")
    if gen.get("licence"):
        return add_build_components(gen["licence"], config_dir, components)
    return licence_summary(config_dir, used, gen, components)


def _details(env: TaskEnv, job_id: str, item: JobItem, run: BuildRun) -> dict[str, Any]:
    store = env.ctx.store
    decision = load_decision(store, job_id, run.inputs["approval_id"])
    b = decision.bound
    cset = load_cset(store, job_id, b["candidate_set_id"])
    qa = load_qa(store, job_id, b["qa_evaluation_id"]) if b.get("qa_evaluation_id") else None
    prompt = load_prompt(store, job_id, b["prompt_revision_id"])
    gen = cset.generation
    used = gen.get("models", [])
    models, provenance = _models_of(env, gen, used)
    return {
        "sources": {"job_id": job_id, "item_id": item.id, "shot_id": item.shot_id, "prompt_revision_id": prompt.id,
                    "candidate_set_id": cset.id, "candidate_id": b["candidate_id"], "approval_id": decision.id,
                    "build_run_id": run.id, "positive": prompt.positive, "negative": prompt.negative,
                    "intermediates": {k: v for k, v in run.artifacts.items() if k in INTERMEDIATE_ROLES},
                    "checkpoints": {k: {"identities": c.identities, "settings": c.settings}
                                    for k, c in run.checkpoints.items()}},
        "config_snapshot_sha": item.snapshot_sha,
        "models": models,
        # The version record has no top-level slot for this, so it rides in `engine`.
        "engine": {**{k: v for k, v in gen.items() if k not in ("models", "model_receipts", "licence")},
                   "models_provenance": provenance},
        "parameters": {"seed": b.get("seed"), **cset.generation.get("params", {})},
        "qa": None if qa is None else {"evaluation_id": qa.id, "status": qa.policy["status"],
                                       "coverage": qa.policy["coverage"], "override": decision.override_qa,
                                       "override_reason": decision.override_reason,
                                       "failed": decision.failed_checks, "missing": decision.missing_checks},
        "validation": run.validation,
        "licence": _licence_of(env, gen, used, run),
    }


def _plan_of(env: TaskEnv, job: Job) -> VariantPlan:
    from ...services.variant_jobs import load_plan

    return load_plan(env.ctx, (job.variant or {})["plan_id"])


def _transform_report(env: TaskEnv, run: BuildRun) -> dict[str, Any] | None:
    meta = run.artifacts.get("meta")
    if not run.build.startswith("direct_") or meta is None:
        return None
    return json.loads(env.ctx.store.artifact_bytes(meta)).get("transform")


def _derivation(env: TaskEnv, job: Job, plan: VariantPlan, run: BuildRun) -> dict[str, Any]:
    v = job.variant or {}
    try:
        fam = get_family(env.ctx.store, v["family_id"])
    except NotFound as e:
        raise ItemFailed(f"family {v['family_id']} is missing", "publication_integrity_failed") from e
    return Derivation(
        method=plan.method, intent=plan.intent, source=plan.source, family_id_at_publication=fam.id,
        family_anchor={"anchor_asset_id": fam.anchor_asset_id, "anchor_version_id": fam.anchor_version_id},
        plan_id=plan.id, plan_sha256=plan.sha256, row_id=v["row_id"], style=plan.style,
        transform=_transform_report(env, run), references=plan.references).model_dump(mode="json")


def _direct_details(env: TaskEnv, job_id: str, item: JobItem, run: BuildRun, plan: VariantPlan) -> dict[str, Any]:
    decision = load_decision(env.ctx.store, job_id, run.inputs["approval_id"])
    src = plan.source
    return {
        "sources": {"job_id": job_id, "item_id": item.id, "approval_id": decision.id, "build_run_id": run.id,
                    "plan_id": plan.id, "row_id": decision.bound["row_id"],
                    "source": {"asset_id": src.asset_id, "version_id": src.version_id}},
        "config_snapshot_sha": item.snapshot_sha, "models": [], "engine": {},
        "parameters": {"transform": decision.bound["transform"]}, "qa": None, "validation": run.validation,
        "licence": derived_licence({"status": "cleared", "components": []}, src.licence,
                                   "deterministic CPU transform"),
    }


def _variant_details(env: TaskEnv, job: Job, item: JobItem, run: BuildRun, plan: VariantPlan) -> dict[str, Any]:
    if job.direct:
        return _direct_details(env, job.id, item, run, plan)
    d = _details(env, job.id, item, run)
    d["licence"] = derived_licence(d["licence"], plan.source.licence,
                                   "source-conditioned image edit + model build")
    return d


def _check_target(item: JobItem, asset_id: str | None, plan: VariantPlan) -> None:
    """A variant is a separate asset: it must never become a version of its source."""
    if plan.source.asset_id in (asset_id, item.target_asset_id):
        raise ItemFailed("a variant cannot be published as a version of its source asset",
                         "publication_integrity_failed")


def publish_item(env: TaskEnv) -> dict[str, Any]:
    t, store = env.task, env.ctx.store
    e = t.inputs
    item, _ = load_item(store, t.job_id, t.item_id)
    run, _ = store.get(build_key(t.job_id, e["build_run_id"]), BuildRun)
    if item.accepted_build != run.id or run.result != "valid":
        raise ItemFailed("only the accepted, valid build result can be published", "stale_input")
    if (done := item.published) is not None and done.build_run_id == run.id:  # another publish already committed it
        return {"asset_id": done.asset_id, "version_id": done.version_id, "display_version": done.display_version}
    snap = store.read_snapshot(item.snapshot_sha)
    kind = Kind(snap["recipe"]["kind"])
    job, _ = load_job(store, t.job_id)
    plan = _plan_of(env, job) if job.variant else None
    if plan is not None:
        _check_target(item, e["asset_id"], plan)
    origin = Origin.derived if plan is not None else Origin.generated
    new_asset = None if e["asset_id"] else NewAsset(
        e["name_id"], item.name, kind, origin, item.category_id, family_id=(job.variant or {}).get("family_id"))
    req = PublishRequest(
        op_id=f"{t.id}", idempotency_key=t.command_id,
        artifacts={k: v for k, v in run.artifacts.items() if k not in INTERMEDIATE_ROLES},
        preview_role="preview" if "preview" in run.artifacts else None, origin=origin, kind=kind,
        asset_id=e["asset_id"], new_asset=new_asset,
        expected_current_version=e.get("expected_current_version"), make_current=e["make_current"],
        details=_variant_details(env, job, item, run, plan) if plan else _details(env, t.job_id, item, run),
        derivation=_derivation(env, job, plan, run) if plan else None)
    try:
        res = publish(store, req)
    except StalePointer as err:
        raise ItemFailed(str(err)[:300], "stale_pointer") from err
    except (Conflict, IntegrityError, StorageError) as err:
        raise ItemFailed(str(err)[:300], getattr(err, "code", "conflict")) from err
    family_name = get_family(store, job.variant["family_id"]).name if job.variant else None
    env.ctx.index.upsert(store.get(manifest_key(res.asset_id), AssetManifest)[0], family_name)

    def apply(x: JobItem) -> None:
        x.published = Published(asset_id=res.asset_id, version_id=res.version_id,
                                display_version=res.display_version, build_run_id=run.id)
        if x.target_asset_id is None:
            x.target_asset_id = res.asset_id
    mutate_item(env.studio, env.ctx, t.job_id, item.id, apply)
    env.studio.events.publish("library", project_id=env.ctx.id, asset_id=res.asset_id, change="published")
    return {"asset_id": res.asset_id, "version_id": res.version_id, "display_version": res.display_version}


__all__ = ["BuildInput", "bake", "derive", "finalize", "preview", "publish_item", "sample", "segment"]
