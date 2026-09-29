"""Selection diversity report for a variant plan: are the CURRENTLY selected variants meaningfully different?

Advisory only. The selection (approved candidate per generative row, or the accepted build preview) is hashed into
a digest; a report belongs to one digest. Changing a selection never touches approvals: the report just goes stale.
Direct-transform rows are excluded (size-only variants may legitimately look identical)."""
from __future__ import annotations

import json
from typing import Any

from assetstudio_core.canonical import sha256_json
from assetstudio_core.ids import derived_id, validate_id
from assetstudio_processing.images import dhash64, hamming64
from pydantic import BaseModel, Field

from ..coordinator.stages import STAGES, new_task
from ..coordinator.stages.diversity import RULESET, report_key
from ..errors import ApiError
from ..registry import ProjectContext
from ..studio import Studio
from ..taskstore import Busy
from . import commands
from .jobs import job_ids
from .records import load_build, load_decision, load_item, load_job

ALL_PAIRS_MAX = 12
SHORTLIST = 96
REPORT_PREFIX = "comparisons/variants"


class CompareSelection(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=100)


def _entry(ctx: ProjectContext, job_id: str, row_id: str, item_id: str) -> dict[str, Any] | None:
    store = ctx.store
    item, _ = load_item(store, job_id, item_id)
    if item.approval is None or item.regen_requested:
        return None
    bound = load_decision(store, job_id, item.approval).bound
    artifact_id, basis = bound["artifact_id"], "candidate"
    if item.accepted_build is not None:
        preview = load_build(store, job_id, item.accepted_build)[0].artifacts.get("preview")
        if preview is not None:
            artifact_id, basis = preview, "build_preview"
    return {"job_id": job_id, "item_id": item_id, "row_id": row_id, "candidate_id": bound["candidate_id"],
            "artifact_id": artifact_id, "sha256": store.artifact(artifact_id).sha256, "basis": basis}


def current_selection(ctx: ProjectContext, plan_id: str) -> list[dict[str, Any]]:
    out = []
    for jid in job_ids(ctx):
        job, _ = load_job(ctx.store, jid)
        v = job.variant or {}
        if v.get("plan_id") != plan_id or job.direct:
            continue
        for iid in job.item_ids:
            if (e := _entry(ctx, jid, v["row_id"], iid)) is not None:
                out.append(e)
    return sorted(out, key=lambda e: (e["job_id"], e["item_id"]))


def shortlist_pairs(hashes: list[int]) -> list[tuple[int, int]]:
    """Index pairs for the VLM: all pairs up to ALL_PAIRS_MAX items, else the SHORTLIST nearest by dHash distance
    (near-duplicates are the ones worth a semantic look)."""
    n = len(hashes)
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    if n <= ALL_PAIRS_MAX:
        return pairs
    ranked = sorted(pairs, key=lambda p: (hamming64(hashes[p[0]], hashes[p[1]]), p))
    return sorted(ranked[:SHORTLIST])


def _deterministic(ctx: ProjectContext, selection: list[dict[str, Any]]) -> tuple[dict[str, Any], list[int]]:
    hashes = [dhash64(ctx.store.artifact_bytes(e["artifact_id"])) for e in selection]
    n = len(selection)
    dups = [[selection[i]["job_id"], selection[j]["job_id"]] for i in range(n) for j in range(i + 1, n)
            if selection[i]["sha256"] == selection[j]["sha256"]]
    dh = [{"a": selection[i]["job_id"], "b": selection[j]["job_id"], "distance": hamming64(hashes[i], hashes[j])}
          for i in range(n) for j in range(i + 1, n)]
    return {"exact_duplicates": dups, "dhash": {e["job_id"]: f"{h:016x}" for e, h in zip(selection, hashes,
                                                                                         strict=True)},
            "dhash_pairs": dh}, hashes


def _plan_compare(ctx: ProjectContext, plan_id: str) -> dict[str, Any]:
    selection = current_selection(ctx, plan_id)
    if len(selection) < 2:
        raise ApiError(409, "selection_too_small", "approve candidates for at least two generative rows first")
    digest = sha256_json(selection)
    det, hashes = _deterministic(ctx, selection)
    pairs = shortlist_pairs(hashes)
    return {"plan_id": plan_id, "selection": selection, "digest": digest, "deterministic": det,
            "pairs": [list(p) for p in pairs], "report_id": derived_id("div", plan_id, digest)}


@commands.replayable("variant_compare_selection")
def _effects(studio: Studio, ctx: ProjectContext, p: dict[str, Any], cid: str) -> dict[str, Any]:
    n = len(p["selection"])
    total = n * (n - 1) // 2
    body = {"plan_id": p["plan_id"], "digest": p["digest"], "report_id": p["report_id"],
            "coverage": "full" if len(p["pairs"]) == total else "partial", "selected": n,
            "pairs": len(p["pairs"]), "total_pairs": total}
    if ctx.store.repo.stat_object(report_key(p["report_id"])) is not None:
        return {**body, "status": "current", "task_id": None}
    first = p["selection"][0]
    task = new_task(studio, STAGES["diversity"], project_id=ctx.id, job_id=first["job_id"], item_id=first["item_id"],
                    input_key=p["digest"], inputs={k: p[k] for k in (
                        "plan_id", "selection", "digest", "deterministic", "pairs", "report_id")})
    try:
        ids = studio.journal.tasks.create([task], cid)
    except Busy as e:
        return {**body, "status": "busy", "task_id": None, "message": str(e)}
    studio.events.publish("tasks", project_id=ctx.id)
    return {**body, "status": "queued", "task_id": ids[0]}


def compare_selection(studio: Studio, ctx: ProjectContext, plan_id: str, req: CompareSelection) -> dict[str, Any]:
    validate_id(plan_id, "vpl")
    from .variant_jobs import load_plan

    load_plan(ctx, plan_id)
    res = commands.execute(studio, ctx, "variant_compare_selection", req.idempotency_key,
                           {"plan_id": plan_id, **req.model_dump(mode="json")}, lambda _c: _plan_compare(ctx, plan_id))
    if res["status"] == "busy":
        raise ApiError(409, "busy", res["message"])
    return res


def _load_report(ctx: ProjectContext, report_id: str) -> dict[str, Any] | None:
    try:
        return json.loads(ctx.store.repo.read_object(report_key(report_id)).data)
    except Exception:  # missing or unreadable: treated as no report
        return None


def _latest_report(ctx: ProjectContext, plan_id: str) -> dict[str, Any] | None:
    found = []
    for rid in ctx.store.list_ids(REPORT_PREFIX):
        if (r := _load_report(ctx, rid)) is not None and r.get("plan_id") == plan_id:
            found.append(r)
    return max(found, key=lambda r: r["created_at"], default=None)


def job_verdicts(report: dict[str, Any]) -> dict[str, str]:
    """pass | fail | unavailable per selected Job: fail if an exact duplicate or any evaluated pair with it is not
    distinct; unavailable if a pair could not be judged or none was evaluated for it."""
    out = {}
    dup = {j for pair in report["deterministic"]["exact_duplicates"] for j in pair}
    for e in report["selection"]:
        jid = e["job_id"]
        mine = [p for p in report["pairs"] if jid in (p["a"], p["b"])]
        if jid in dup or any(p["result"] == "fail" for p in mine):
            out[jid] = "fail"
        elif not mine or any(p["result"] == "unavailable" for p in mine):
            out[jid] = "unavailable"
        else:
            out[jid] = "pass"
    return out


def diversity_status(ctx: ProjectContext, plan_id: str) -> dict[str, Any]:
    validate_id(plan_id, "vpl")
    from .variant_jobs import load_plan

    load_plan(ctx, plan_id)
    selection = current_selection(ctx, plan_id)
    digest = sha256_json(selection) if selection else None
    current = _load_report(ctx, derived_id("div", plan_id, digest)) if digest else None
    report = current or _latest_report(ctx, plan_id)
    status = "current" if current else ("stale" if report else "missing")
    return {"plan_id": plan_id, "status": status, "ruleset": RULESET, "selection_digest": digest,
            "selected": len(selection), "report": report,
            "jobs": job_verdicts(report) if report else {}}
