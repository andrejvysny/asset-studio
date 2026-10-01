"""One slot-eligibility evaluator (H16): preflight readiness, placement, acquire and accept all ask `eligible`.

It covers the declarative, per-call requirements (capability, engine, export features/exporter, model receipts) and
runner freshness. Transient occupancy (device claims, a slot already holding an attempt) stays in `slot_problem`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from assetstudio_protocol.execution import OPERATION_CAPABILITY, OPERATION_ENGINE, OPERATION_VERSIONS, Requirements
from assetstudio_protocol.inventory import Inventory, Slot

from ..studio import Studio
from .placement import _SLOT_STATES, _model_problem, catalog_sha256, session_fresh

__all__ = ["Requirement", "eligible", "requirement_for", "slot_eligible"]

FEATURE_PREFIX = "export-feature."
RESEARCH_LABEL = "exporter-research"
GEOMETRY_FEATURE = "geometry_policy.v1"
_GEOMETRY_KEYS = ("small_components", "fill_holes")


@dataclass(frozen=True)
class Requirement:
    operation: str
    capability: str
    engine: str
    export_features: frozenset[str] = frozenset()
    exporter: str | None = None
    models: frozenset[str] = frozenset()


def requirement_for(operation: str, params: dict[str, Any] | None = None,
                    requirements: Requirements | None = None) -> Requirement:
    """Export needs follow the call's own params: a geometry-cleanup key needs `geometry_policy.v1`, a
    non-default exporter needs its label. Models come from the offer's Requirements when the caller has them."""
    inner = (params or {}).get("params")
    p = inner if isinstance(inner, dict) else (params or {})
    features: frozenset[str] = frozenset()
    exporter: str | None = None
    if operation == "worker3d.export":
        if any(p.get(k) for k in _GEOMETRY_KEYS):
            features = frozenset({GEOMETRY_FEATURE})
        exporter = p.get("exporter") if p.get("exporter") not in (None, "clean") else None
    return Requirement(operation, OPERATION_CAPABILITY[operation], OPERATION_ENGINE[operation], features, exporter,
                       frozenset(requirements.models) if requirements else frozenset())


def eligible(name: str, inventory: Inventory, slot: Slot, req: Requirement, *, fresh: bool,
             catalog_sha: str | None = None) -> tuple[bool, list[str]]:
    """(ok, every unsatisfied requirement). `name` is the runner's name, for the reasons only."""
    why: list[str] = []
    if not fresh:
        why.append(f"runner {name} not fresh")
    if slot.capability != req.capability:
        why.append(f"slot {slot.slot_id} wrong capability")
    wanted = {"op": req.operation, "version": OPERATION_VERSIONS[req.operation]}
    engine = next((e for e in slot.engines if e.engine == req.engine), None)
    if engine is None:
        why.append(f"engine {req.engine} not on slot {slot.slot_id}")
    elif wanted not in [o.model_dump() for o in engine.operations]:
        why.append(f"no engine offers {req.operation} v{wanted['version']} on slot {slot.slot_id}")
    if slot.state not in _SLOT_STATES:
        why.append(f"slot {slot.slot_id} state {slot.state}")
    labels = set(inventory.labels)
    why += [f"slot {slot.slot_id} lacks {FEATURE_PREFIX}{f}" for f in sorted(req.export_features)
            if FEATURE_PREFIX + f not in labels]
    if req.exporter == "research" and RESEARCH_LABEL not in labels:
        why.append(f"slot {slot.slot_id} lacks {RESEARCH_LABEL}")
    if req.models and catalog_sha is not None:
        receipts = {m.key: m.model_dump() for m in inventory.models}
        why += [p for i in sorted(req.models) if (p := _model_problem(i, receipts, catalog_sha))]
    return not why, why


def slot_eligible(studio: Studio, runner: dict[str, Any], slot_id: str, req: Requirement, *,
                  session: dict[str, Any] | None = None) -> tuple[bool, list[str]]:
    """`eligible` for a runner's current inventory as the journal holds it (placement, acquire, accept)."""
    name = runner["name"]
    session = session or studio.journal.runners.active_session(runner["id"])
    if session is None or session["inventory"] is None:
        return False, [f"runner {name} has no active inventory"]
    inv = Inventory.model_validate(session["inventory"])
    slot = next((s for s in inv.slots if s.slot_id == slot_id), None)
    if slot is None:
        return False, [f"slot {slot_id} no longer advertised by runner {name}"]
    return eligible(name, inv, slot, req, fresh=session_fresh(studio, session),
                    catalog_sha=catalog_sha256(studio) if req.models else None)
