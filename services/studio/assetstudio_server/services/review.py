"""Review gates: exact candidate approval, deliberate bulk proposals, final acceptance.

Units may come from several Jobs (a Batch wave); each unit keeps its own immutable decision record."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from assetstudio_core.canonical import now_iso
from assetstudio_core.domain import BuildRun, JobItem, ReviewDecision
from assetstudio_core.ids import derived_id
from assetstudio_core.review import ApprovalRequest, ReviewError, check_binding, propose_best
from assetstudio_storage.repo import CorruptBlob
from pydantic import BaseModel, Field

from ..actor import OPERATOR
from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from . import commands
from .build_binding import build_matches_approval, restorable_build
from .production import refuse_direct
from .prompts import ItemRef, job_of, outcome
from .records import decision_key, load_build, load_cset, load_item, load_items, load_job, load_qa
from .runs import active_run_for, record_wave
from .taskview import busy, item_tasks


class ApproveItem(ItemRef):
    candidate_set_id: str
    candidate_id: str
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_revision_id: str
    qa_evaluation_id: str | None
    override_qa: bool = False
    override_reason: str | None = Field(default=None, max_length=1000)


class Approve(BaseModel):
    items: list[ApproveItem] = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=100)


class ClearApproval(BaseModel):
    items: list[ItemRef] = Field(min_length=1, max_length=500)


class PreviewBest(BaseModel):
    item_ids: list[str] | None = None


class AcceptItem(ItemRef):
    build_run_id: str
    accept: bool = True


class AcceptBuilds(BaseModel):
    items: list[AcceptItem] = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=100)


def _blob_sha(ctx: ProjectContext, artifact_id: str) -> str | None:
    """Re-hash the stored bytes (no cache): the approval binds content, not a filename or a cached claim."""
    art = ctx.store.artifact(artifact_id)
    try:
        ctx.store.repo.verify_blob(art.sha256, art.size, use_cache=False)
    except CorruptBlob:
        return None
    return art.sha256


def _plan_approve_unit(studio: Studio, ctx: ProjectContext, jid: str, a: ApproveItem, cid: str,
                       run_id: str | None) -> dict[str, Any]:
    """Validates one unit against the current records and returns its immutable decision payload (no writes)."""
    item, _ = load_item(ctx.store, jid, a.item_id)
    cset = load_cset(ctx.store, jid, a.candidate_set_id) if a.candidate_set_id in item.candidate_sets else None
    qa = load_qa(ctx.store, jid, a.qa_evaluation_id) if a.qa_evaluation_id else None
    cand = next((c for c in cset.candidates if c.id == a.candidate_id), None) if cset else None
    payload = check_binding(item, cset, qa, ApprovalRequest(
        item_id=item.id, candidate_set_id=a.candidate_set_id, candidate_id=a.candidate_id,
        image_sha256=a.image_sha256, prompt_revision_id=a.prompt_revision_id,
        qa_evaluation_id=a.qa_evaluation_id, expected_item_revision=a.expected_item_revision,
        override_qa=a.override_qa, override_reason=a.override_reason),
        _blob_sha(ctx, cand.artifact_id) if cand else None,
        build_busy=busy(item_tasks(studio, ctx.id, item), "build"))
    payload["bound"]["restored_build_run_id"] = restorable_build(
        ctx.store, jid, item, (a.candidate_set_id, a.candidate_id, a.image_sha256))
    return {"job_id": jid, "item_id": a.item_id, "expected_item_revision": a.expected_item_revision,
            "decision_id": derived_id("dec", cid, item.id), "decision": payload,
            "run_id": run_id or active_run_for(studio, ctx, jid)}


def _failed(jid: str | None, item_id: str, code: str, message: str,
            detail: dict[str, Any] | None = None) -> dict[str, Any]:
    r: dict[str, Any] = {"job_id": jid, "item_id": item_id, "ok": False, "code": code, "message": message}
    if detail is not None:
        r["detail"] = detail
    return r


Plan = tuple[list[dict[str, Any] | None], list[dict[str, Any]]]


def _plan_units(req_items: list[Any], job_id: str | None,
                unit_fn: Callable[[str, Any], dict[str, Any]]) -> Plan:
    """Per request unit, in order: a failed result, or None (a planned unit follows in `units`)."""
    results: list[dict[str, Any] | None] = []
    units: list[dict[str, Any]] = []
    for a in req_items:
        jid = None
        try:
            jid = job_of(a, job_id)
            unit = unit_fn(jid, a)
        except ReviewError as e:
            results.append(_failed(jid, a.item_id, e.code, str(e), e.detail))
            continue
        except ApiError as e:
            results.append(_failed(jid, a.item_id, e.code, e.message))
            continue
        results.append(None)
        units.append(unit)
    return results, units


def _merge(results: list[dict[str, Any] | None], outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    it = iter(outcomes)
    return [r if r is not None else next(it) for r in results]


def _restore_and_approve(item: JobItem, u: dict[str, Any], tasks: Any) -> None:
    if item.cancelled:
        raise ApiError(409, "item_cancelled", "item is cancelled")
    if item.accepted_build is not None or busy(tasks, "build"):
        raise ApiError(409, "build_in_progress", "a build is running or accepted for this item")
    did = u["decision_id"]
    if did not in item.decisions:
        item.decisions.append(did)
    item.approval = did
    item.regen_requested = False
    item.current_build = u["decision"]["bound"].get("restored_build_run_id")


@commands.replayable("approve")
def _approve_effects(studio: Studio, ctx: ProjectContext, plan: dict[str, Any], cid: str) -> dict[str, Any]:
    outcomes = []
    for u in plan["units"]:
        jid, iid, did = u["job_id"], u["item_id"], u["decision_id"]
        item, _ = load_item(ctx.store, jid, iid)
        if did in item.decisions:  # replay after the item write
            outcomes.append({"job_id": jid, "item_id": iid, "ok": True, "revision": item.revision})
            continue
        if item.revision != u["expected_item_revision"]:
            outcomes.append(_failed(jid, iid, "stale_item", f"{item.name} changed (revision {item.revision}); reload"))
            continue
        if ctx.store.repo.stat_object(decision_key(jid, did)) is None:
            ctx.store.create(decision_key(jid, did), ReviewDecision(
                id=did, gate="candidate_approval", job_id=jid, item_id=iid, decided_at=now_iso(),
                idempotency_key=plan["idempotency_key"], run_id=u["run_id"], wave_id=plan.get("wave_id"),
                actor=plan.get("actor") or OPERATOR, **u["decision"]))
        outcomes.append(outcome(studio, ctx, jid, iid, lambda x, u=u: _restore_and_approve(
            x, u, item_tasks(studio, ctx.id, x)), u["expected_item_revision"]))
    _record(studio, ctx, plan, cid, "candidate_approval", outcomes, lambda u: {"approval_id": u["decision_id"]})
    return {"results": _merge(plan["results"], outcomes)}


def _record(studio: Studio, ctx: ProjectContext, plan: dict[str, Any], cid: str, gate: str,
            outcomes: list[dict[str, Any]], wave_unit: Callable[[dict[str, Any]], dict[str, Any] | None]) -> None:
    """Wave record of the units that took effect; `wave_unit` maps a plan unit to its extra record fields."""
    units = []
    for u, o in zip(plan["units"], outcomes, strict=True):
        extra = wave_unit(u) if o["ok"] else None
        if extra is not None:
            units.append({"job_id": u["job_id"], "item_id": u["item_id"], **extra})
    record_wave(studio, ctx, {"run_id": plan["run_id"], "wave_id": plan.get("wave_id"), "units": units,
                              "actor": plan.get("actor")}, cid, gate)


def approve(studio: Studio, ctx: ProjectContext, job_id: str | None, req: Approve,
            run_id: str | None = None) -> dict[str, Any]:
    ctx.require_writable()
    body = {"job_id": job_id, "run_id": run_id, **req.model_dump(mode="json")}

    def plan(cid: str) -> dict[str, Any]:
        if job_id is not None:
            refuse_direct(ctx, job_id)

        def unit(jid: str, a: ApproveItem) -> dict[str, Any]:
            refuse_direct(ctx, jid)
            return _plan_approve_unit(studio, ctx, jid, a, cid, run_id)
        results, units = _plan_units(req.items, job_id, unit)
        return {"results": results, "units": units, "run_id": run_id, "idempotency_key": req.idempotency_key,
                "wave_id": derived_id("wav", cid) if run_id else None}
    return commands.execute(studio, ctx, "approve", req.idempotency_key, body, plan)


def clear_approval(studio: Studio, ctx: ProjectContext, job_id: str | None,
                   req: ClearApproval) -> list[dict[str, Any]]:
    out = []
    for r in req.items:
        jid = job_of(r, job_id)
        tasks = item_tasks(studio, ctx.id, load_item(ctx.store, jid, r.item_id)[0])

        def apply(item: JobItem, tasks: Any = tasks) -> None:
            if busy(tasks, "build"):
                raise ApiError(409, "build_in_progress", "cancel the running build first")
            if item.accepted_build is not None:
                raise ApiError(409, "accepted", "the build result is accepted; unaccept it first")
            item.approval = None
            item.current_build = None
        out.append(outcome(studio, ctx, jid, r.item_id, apply, r.expected_item_revision))
    return out


def preview_best(ctx: ProjectContext, job_ids: list[str], req: PreviewBest) -> dict[str, Any]:
    """A proposal only: nothing is approved until a human confirms exact candidates."""
    proposals = []
    for jid in job_ids:
        job, _ = load_job(ctx.store, jid)
        for item in load_items(ctx.store, job):
            if req.item_ids is not None and item.id not in req.item_ids:
                continue
            cset = load_cset(ctx.store, jid, item.current_set) if item.current_set else None
            qas = {qid: load_qa(ctx.store, jid, qid) for qid in item.qa.values()}
            proposals.append({**propose_best(item, cset, qas), "name": item.name, "job_id": jid})
    return {"policy": "fewest failed minor checks, then fewest unavailable, then candidate order; recommended only",
            "proposals": [p for p in proposals if "skip" not in p],
            "skipped": [p for p in proposals if "skip" in p]}


def _accept_guard(studio: Studio, ctx: ProjectContext, jid: str, item: JobItem, build_run_id: str,
                  accept: bool) -> BuildRun:
    if build_run_id not in item.build_runs:
        raise ApiError(409, "stale_build", "the build result changed; reload")
    run, _ = load_build(ctx.store, jid, build_run_id)
    if not accept:
        if item.current_build != build_run_id:
            raise ApiError(409, "stale_build", "the build result changed; reload")
        if busy(item_tasks(studio, ctx.id, item), "publish"):
            raise ApiError(409, "publishing", "publication is in progress")
        return run
    if run.status != "succeeded" or run.result != "valid":
        raise ApiError(409, "invalid_build", f"build is {run.status}/{run.result}: only valid results "
                                             "can be accepted")
    if not build_matches_approval(ctx.store, jid, item, run):
        raise ApiError(409, "approval_mismatch", "this attempt was built from a different candidate than the "
                                                 "current approval; approve that candidate again or build again")
    if item.current_build != build_run_id:
        raise ApiError(409, "stale_build", "the build result changed; reload")
    return run


def _plan_accept_unit(studio: Studio, ctx: ProjectContext, jid: str, a: AcceptItem, cid: str,
                      run_id: str | None) -> dict[str, Any]:
    item, _ = load_item(ctx.store, jid, a.item_id)
    if item.revision != a.expected_item_revision:
        raise ApiError(409, "stale_item", f"{item.name} changed (revision {item.revision}); reload")
    run = _accept_guard(studio, ctx, jid, item, a.build_run_id, a.accept)
    return {"job_id": jid, "item_id": a.item_id, "expected_item_revision": a.expected_item_revision,
            "build_run_id": a.build_run_id, "accept": a.accept, "artifacts": run.artifacts,
            "decision_id": derived_id("dec", cid, item.id), "run_id": run_id or active_run_for(studio, ctx, jid)}


def _apply_accept(studio: Studio, ctx: ProjectContext, item: JobItem, u: dict[str, Any]) -> None:
    _accept_guard(studio, ctx, u["job_id"], item, u["build_run_id"], u["accept"])
    if not u["accept"]:
        item.accepted_build = None
        return
    if u["decision_id"] not in item.decisions:
        item.decisions.append(u["decision_id"])
    item.accepted_build = u["build_run_id"]


def _accept_applied(item: JobItem, u: dict[str, Any]) -> bool:
    if u["accept"]:
        return u["decision_id"] in item.decisions
    return (item.accepted_build is None and item.current_build == u["build_run_id"]
            and item.revision != u["expected_item_revision"])


@commands.replayable("accept_builds")
def _accept_effects(studio: Studio, ctx: ProjectContext, plan: dict[str, Any], cid: str) -> dict[str, Any]:
    outcomes = []
    for u in plan["units"]:
        jid, iid, did = u["job_id"], u["item_id"], u["decision_id"]
        item, _ = load_item(ctx.store, jid, iid)
        if _accept_applied(item, u):  # replay after the item write
            outcomes.append({"job_id": jid, "item_id": iid, "ok": True, "revision": item.revision})
            continue
        if u["accept"] and item.revision == u["expected_item_revision"] and (
                ctx.store.repo.stat_object(decision_key(jid, did)) is None):
            ctx.store.create(decision_key(jid, did), ReviewDecision(
                id=did, gate="final_acceptance", job_id=jid, item_id=iid,
                bound={"build_run_id": u["build_run_id"], "artifacts": u["artifacts"]}, decided_at=now_iso(),
                idempotency_key=plan["idempotency_key"], run_id=u["run_id"], wave_id=plan.get("wave_id"),
                actor=plan.get("actor") or OPERATOR))
        outcomes.append(outcome(studio, ctx, jid, iid, lambda x, u=u: _apply_accept(studio, ctx, x, u),
                                u["expected_item_revision"]))
    _record(studio, ctx, plan, cid, "final_acceptance", outcomes,
            lambda u: {"build_run_id": u["build_run_id"]} if u["accept"] else None)
    return {"results": _merge(plan["results"], outcomes)}


def accept_builds(studio: Studio, ctx: ProjectContext, job_id: str | None, req: AcceptBuilds,
                  run_id: str | None = None) -> dict[str, Any]:
    """Final human acceptance of an actual built result. Structural validity is mandatory, never overridable."""
    ctx.require_writable()
    body = {"job_id": job_id, "run_id": run_id, **req.model_dump(mode="json")}

    def plan(cid: str) -> dict[str, Any]:
        results, units = _plan_units(req.items, job_id, lambda jid, a: _plan_accept_unit(
            studio, ctx, jid, a, cid, run_id))
        return {"results": results, "units": units, "run_id": run_id, "idempotency_key": req.idempotency_key,
                "wave_id": derived_id("wav", cid) if run_id else None}
    return commands.execute(studio, ctx, "accept_builds", req.idempotency_key, body, plan)
