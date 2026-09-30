"""Engine recovery barrier (R7): a slot is advertised ready only after engines proved they hold no stray work."""
from __future__ import annotations

from typing import Any

from assetstudio_protocol.engine import EngineUnavailable
from assetstudio_protocol.inventory import SlotState

from .config import RunnerConfig, SlotConfig
from .engine_executor import EngineExecutor
from .executor import Executor
from .state import RunnerState

_LIVE = ("admitted", "executing")


def _local_prompts(state: RunnerState, slot_id: str) -> set[str]:
    return {str(a.offer.params["prompt_id"]) for a in state.list_attempts()
            if a.state in _LIVE and a.offer.slot_id == slot_id and a.offer.operation.startswith("image.")
            and "prompt_id" in a.offer.params}


def _image_slot(sc: SlotConfig, executor: EngineExecutor, state: RunnerState) -> SlotState:
    engine: Any = executor.engines.comfy
    if engine is None:
        return "unreachable"
    local = _local_prompts(state, sc.slot_id)
    try:
        for prompt_id in local:  # reconcile by id: also proves the engine answers lookups
            engine.status(prompt_id)
        queued = engine.queue_prompt_ids()
    except EngineUnavailable:
        return "unreachable"
    return "unknown" if queued - local else "ready"  # foreign queue entries block until drained or reset


def _aux3d_slot(sc: SlotConfig, executor: EngineExecutor) -> SlotState:
    lane = executor.lanes[sc.slot_id]
    if not lane.workers:
        return "unreachable"
    return "ready" if lane.reset()["ok"] else "unknown"


def recover_slots(config: RunnerConfig, executor: Executor, state: RunnerState,
                  only: set[str] | None = None) -> dict[str, SlotState]:
    """Slot states after the barrier. Executors without engines (the fake one) have nothing to recover."""
    out: dict[str, SlotState] = {}
    for sc in config.slots:
        if only is not None and sc.slot_id not in only:
            continue
        if not isinstance(executor, EngineExecutor):
            out[sc.slot_id] = "ready"
        elif sc.capability == "image":
            out[sc.slot_id] = _image_slot(sc, executor, state)
        else:
            out[sc.slot_id] = _aux3d_slot(sc, executor)
    return out
