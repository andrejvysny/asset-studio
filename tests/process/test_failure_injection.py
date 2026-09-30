"""Failure injection across real OS processes (docs/modular/compute-runner.md R6-R8; acceptance A07-A10).

Every scenario kills a process with SIGKILL (no cleanup handlers run) and proves what Studio, the runner and the
engine each keep or recover. Waits poll observable state through the public API, the runner's own sqlite state and
the fake engine's request log; nothing relies on sleeping for a duration to pass.
"""
from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from assetstudio_node.admission import OwnershipUnknown
from assetstudio_node.barrier import recover_slots
from assetstudio_node.engines.base import EngineUnavailable

from tests.process.conftest import Engine, Proc, World, wait_until
from tests.process.old_agent import SLOT, make_executor

pytestmark = pytest.mark.process


def _in_flight(world: World) -> list[dict]:
    return [r for r in world.engine.requests() if r["ended_at"] is None]


def _first_attempt_executing(world: World) -> dict:
    """The aux call is executing on the runner AND its request is parked inside the engine process."""
    a = wait_until(lambda: world.attempt(state="executing"), "attempt executing\n" + world.diagnostics())
    wait_until(lambda: _in_flight(world), "request in flight inside the engine\n" + world.diagnostics())
    return a


def test_a07_studio_killed_mid_attempt_reconciles_without_replacement(world: World) -> None:
    world.engine.control(hold=True)
    world.start_job()
    first = _first_attempt_executing(world)

    world.studio.kill9()
    world.studio.start()  # same instance dir, same port: the runner just sees Studio come back

    wait_until(lambda: world.enhance_task()["state"] == "reconciling", "task reconciling after restart\n"
               + world.diagnostics())
    # Several maintenance cycles with the runner undelivered: the attempt must stay put, never be re-placed.
    end = time.monotonic() + 3
    while time.monotonic() < end:
        rows = world.attempts()
        assert [(a["id"], a["generation"], a["runner_id"]) for a in rows] == [
            (first["id"], 1, first["runner_id"])], rows
        assert world.enhance_task()["state"] == "reconciling"
        time.sleep(0.3)

    world.engine.control(hold=False)  # the engine finishes; the runner can deliver
    wait_until(lambda: world.enhance_task()["state"] == "succeeded", "task succeeded after delivery\n"
               + world.diagnostics())
    (only,) = world.attempts()
    assert (only["id"], only["generation"], only["state"]) == (first["id"], 1, "committed")
    assert len(world.engine.requests()) == 1  # the engine computed it exactly once
    wait_until(lambda: world.runner.local_attempts() == [] and world.runner.spool_dirs() == [],
               "runner spool and state empty\n" + world.diagnostics())


def test_a08_runner_killed_after_spooling_recovers_output(world: World) -> None:
    """Studio is taken down first so the runner's upload cannot finish: its local state then provably sits at
    `spooled`/`uploading` with the output on disk, which is the moment the runner is SIGKILLed."""
    world.engine.control(hold=True)
    world.start_job()
    first = _first_attempt_executing(world)
    world.studio.kill9()
    world.engine.control(hold=False)  # the result is computed; the runner spools it but cannot deliver
    wait_until(lambda: [s for _, s in world.runner.local_attempts()] in (["spooled"], ["uploading"])
               and world.runner.spool_dirs() == [first["id"]], "output spooled, delivery pending\n"
               + world.diagnostics())
    world.runner.kill9()

    world.studio.start()
    world.runner.start()  # same state dir: hello lists the spooled attempt, the agent resumes its delivery
    wait_until(lambda: world.enhance_task()["state"] == "succeeded", "task succeeded from the recovered output\n"
               + world.diagnostics())
    (only,) = world.attempts()
    assert (only["id"], only["generation"], only["state"]) == (first["id"], 1, "committed")
    assert len(world.engine.requests()) == 1  # recovered from the spool, not recomputed
    wait_until(lambda: world.runner.local_attempts() == [] and world.runner.spool_dirs() == [],
               "spool empty after the disposition receipt\n" + world.diagnostics())
    assert set(world.device_claims().values()) == {"free"}


def test_a09_a10_runner_killed_while_executing_is_uncertain_until_barrier_evidence(world: World) -> None:
    world.engine.control(hold=True)
    world.start_job()
    first = _first_attempt_executing(world)
    world.runner.kill9()

    # A10: the lease lapses -> uncertain; the device is NOT freed by a timeout (R6).
    wait_until(lambda: world.attempt(id=first["id"], state="uncertain"), "attempt uncertain after lease expiry\n"
               + world.diagnostics())
    assert set(world.device_claims().values()) == {"uncertain"}
    assert [a["generation"] for a in world.attempts()] == [1]  # no new generation without an operator decision
    assert world.enhance_task()["state"] not in ("succeeded", "failed")

    world.engine.control(hold=False)  # the engine outlived its agent and now finishes (R7); it is idle again
    wait_until(lambda: not _in_flight(world), "engine request finished\n" + world.diagnostics())

    # Operator declares the attempt lost: a new generation is offered, but the device stays uncertain (no runner).
    world.studio.post(f"/api/v1/attempts/{first['id']}:declare-lost")
    wait_until(lambda: world.remote_call()["generation"] == 2, "generation 2 offered\n" + world.diagnostics())
    assert world.attempt(id=first["id"])["state"] == "lost"
    time.sleep(1.5)  # maintenance keeps trying to place it; it must not reach the device
    gen2 = world.remote_call()
    assert (gen2["state"], gen2["runner_id"]) == ("offered", None)
    assert set(world.device_claims().values()) == {"uncertain"}
    assert len(world.engine.requests()) == 1  # nothing ran twice while uncertain

    # A09: the restarted runner re-advertises through the barrier (fresh inventory): claim released, gen 2 runs.
    world.runner.start()
    wait_until(lambda: world.enhance_task()["state"] == "succeeded", "generation 2 completed\n" + world.diagnostics())
    by_gen = {a["generation"]: a for a in world.attempts()}
    assert by_gen[1]["state"] == "lost" and by_gen[2]["state"] == "committed"
    assert set(world.device_claims().values()) == {"free"}
    assert len(world.engine.requests()) == 2  # one computation per generation


def test_r7_engine_outlives_killed_agent_and_blocks_new_admission_until_idle(
        tmp_path: Path, fake_engine_proc: Callable[..., Engine]) -> None:
    """R7 across the process boundary: the old agent (its own process) is SIGKILLed with a request in flight inside
    the engine process. A new agent's barrier must not call the slot ready, nor admit work, until that request ends."""
    engine = fake_engine_proc("aux", drain_timeout=1.0)
    state_dir = tmp_path / "survivor-state"
    engine.control(hold=True)
    old = Proc("old-agent", [sys.executable, "-m", "tests.process.old_agent", str(state_dir), engine.url],
               {**os.environ}, tmp_path / "old-agent.log")
    try:
        old.start()
        (req,) = wait_until(lambda: [r for r in engine.requests() if r["ended_at"] is None] or None,
                            f"old agent's request in flight\n{old.tail()}")
        old_epoch = req["epoch"]
        old.kill9()
        assert engine.log_()["lease"]["active"] == 1  # the agent is gone; the engine still holds its work
    finally:
        old.stop()

    config, state, executor = make_executor(state_dir, engine.url)
    try:
        # New agent, engine busy: the barrier cannot get a release acknowledgement -> not ready (R7).
        assert recover_slots(config, executor, state) == {SLOT: "unknown"}
        with pytest.raises(OwnershipUnknown):
            executor.lanes[SLOT].acquire("aux")  # no grant, hence no admission, while the old request is active
        assert [(r["epoch"], r["ended_at"] is None) for r in engine.requests()] == [(old_epoch, True)]
        # Even a caller that guessed the next epoch is fenced: the engine stopped admitting when it began draining.
        with pytest.raises(EngineUnavailable):
            executor.engines.aux.enhance(brief="late", kind="concept_art", constraints="", style_guide="",
                                         epoch=old_epoch + 1, execution_id="exec-new")
        assert len(engine.requests()) == 1

        engine.control(hold=False)  # the old request finishes on its own
        wait_until(lambda: engine.log_()["lease"]["active"] == 0, "old request finished")
        assert recover_slots(config, executor, state) == {SLOT: "ready"}
        epoch = executor.lanes[SLOT].acquire("aux")
        assert epoch > old_epoch
        executor.engines.aux.enhance(brief="new agent", kind="concept_art", constraints="", style_guide="",
                                     epoch=epoch, execution_id="exec-new")
    finally:
        state.close()

    old_row, new_row = engine.requests()
    assert old_row["epoch"] == old_epoch and new_row["epoch"] == epoch
    assert new_row["started_at"] >= old_row["ended_at"]  # nothing of the new epoch ran while the old one was active
