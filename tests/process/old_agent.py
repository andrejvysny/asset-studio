"""The 'old agent' of the engine-survival scenario, run as its own OS process so it can be SIGKILLed mid-request.

It builds the runner's real EngineExecutor (real HTTP AuxClient, persisted GPU-lane epochs in the state dir), takes the
lane for aux and issues one aux.enhance under that epoch. The call blocks inside the engine process until released.
Usage: python -m tests.process.old_agent STATE_DIR AUX_URL
"""
from __future__ import annotations

import sys
from pathlib import Path

from assetstudio_node.config import RunnerConfig
from assetstudio_node.engine_executor import EngineExecutor, build_engines
from assetstudio_node.state import RunnerState

SLOT = "gpu-aux"


def make_executor(state_dir: Path, aux_url: str) -> tuple[RunnerConfig, RunnerState, EngineExecutor]:
    config = RunnerConfig.model_validate({
        "studio_url": "http://127.0.0.1:1", "name": "survivor", "state_dir": str(state_dir), "simulated": False,
        "models_root": str(state_dir), "engines": {"aux": aux_url},
        "slots": [{"slot_id": SLOT, "capability": "aux3d", "devices": ["index:0"], "engines": ["aux"]}]})
    state = RunnerState(state_dir)
    return config, state, EngineExecutor(config, state, build_engines(config))


def main() -> None:
    state_dir, aux_url = Path(sys.argv[1]), sys.argv[2]
    _, _, executor = make_executor(state_dir, aux_url)
    epoch = executor.lanes[SLOT].acquire("aux")
    executor.engines.aux.enhance(brief="old agent call", kind="concept_art", constraints="", style_guide="",
                                 epoch=epoch, execution_id="exec-old-agent")


if __name__ == "__main__":
    main()
