"""Runner agent concurrency (H11): per-slot threads, bounded transfer pool, device exclusivity, idle-only acquire."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from assetstudio_node import agent as agent_mod
from assetstudio_node.agent import RunnerAgent
from assetstudio_node.config import RunnerConfig
from assetstudio_node.executor import FakeExecutor
from assetstudio_node.slots import SlotSupervisor
from assetstudio_node.spool import Spool
from assetstudio_node.state import RunnerState
from assetstudio_protocol.execution import Offer

from tests.unit.test_runner_agent import TOKEN, Clock, StubClient, cfg_dict, make_offer, slot

WAIT_S = 10.0


class Gauge:
    """Counts how many callers are inside a section at once."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.now = 0
        self.peak = 0

    def enter(self) -> None:
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)

    def leave(self) -> None:
        with self.lock:
            self.now -= 1


class GateExecutor(FakeExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.gauge = Gauge()
        self.entered: dict[str, threading.Event] = {}
        self.gates: dict[str, threading.Event] = {}
        self.lock = threading.Lock()

    def ev(self, table: dict[str, threading.Event], aid: str) -> threading.Event:
        with self.lock:
            return table.setdefault(aid, threading.Event())

    def execute(self, offer: Offer, inputs: Any, out_dir: Path, should_cancel: Callable[[], bool]) -> Any:
        self.gauge.enter()
        try:
            self.ev(self.entered, offer.attempt_id).set()
            assert self.ev(self.gates, offer.attempt_id).wait(WAIT_S)
            return super().execute(offer, inputs, out_dir, should_cancel)
        finally:
            self.gauge.leave()


class SlotStub(StubClient):
    """Hands out the first queued offer whose slot is advertised free; uploads can be gated per attempt."""

    def __init__(self) -> None:
        super().__init__()
        self.free_history: list[list[str]] = []
        self.upload_gauge = Gauge()
        self.upload_started: dict[str, threading.Event] = {}
        self.upload_gates: dict[str, threading.Event] = {}
        self.gate_uploads = False
        self.lock = threading.Lock()

    def ev(self, table: dict[str, threading.Event], aid: str) -> threading.Event:
        with self.lock:
            return table.setdefault(aid, threading.Event())

    def acquire(self, sid: str, req: Any) -> Offer | None:
        self.calls.append(("acquire", req))
        self.free_history.append(list(req.free_slots))
        for o in list(self.offers):
            if o.slot_id in req.free_slots:
                self.offers.remove(o)
                return o
        return None

    def upload_file(self, path: Path, *, attempt_id: str, generation: int, role: str, mime: str) -> None:
        self.upload_gauge.enter()
        try:
            self.ev(self.upload_started, attempt_id).set()
            if self.gate_uploads:
                assert self.ev(self.upload_gates, attempt_id).wait(WAIT_S)
            super().upload_file(path, attempt_id=attempt_id, generation=generation, role=role, mime=mime)
        finally:
            self.upload_gauge.leave()


def make(tmp_path: Path, slots: list[dict[str, Any]], *, executor: GateExecutor | None = None,
         shared_device: str | None = None, idle_s: float = 0.0, **cfg: Any) -> tuple[RunnerAgent, SlotStub, GateExecutor]:
    stub, executor = SlotStub(), executor or GateExecutor()
    config = RunnerConfig.model_validate(cfg_dict(tmp_path, slots=slots, **cfg))
    state = RunnerState(config.state_dir)
    agent = RunnerAgent(config, client=stub, executor=executor, state=state, spool=Spool(config.state_dir / "spool"),
                        clock=Clock(), idle_s=idle_s)  # type: ignore[arg-type]
    agent.bootstrap(TOKEN)
    agent.open_session()
    if shared_device:  # validator and inventory forbid overlap; the agent must still hold the line by itself
        for s in config.slots:
            s.devices = [shared_device]
        agent._sup = SlotSupervisor(config)
    return agent, stub, executor


def offer_for(slot_id: str) -> Offer:
    return make_offer().model_copy(update={"slot_id": slot_id})


def drive(agent: RunnerAgent, cond: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while not cond():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        agent.step()
        time.sleep(0.005)


def wait_for(cond: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while not cond():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.005)


def open_all(ex: GateExecutor, offers: list[Offer]) -> None:
    for o in offers:
        ex.ev(ex.gates, o.attempt_id).set()


def completed(stub: StubClient) -> int:
    return len(stub.names("complete"))


def test_independent_slots_execute_concurrently(tmp_path: Path) -> None:
    agent, stub, ex = make(tmp_path, [slot("gpu0", ["GPU-a"]), slot("gpu1", ["GPU-b"])])
    offers = [offer_for("gpu0"), offer_for("gpu1")]
    stub.offers.extend(offers)
    drive(agent, lambda: all(ex.ev(ex.entered, o.attempt_id).is_set() for o in offers), "both inside execute")
    assert ex.gauge.now == 2
    open_all(ex, offers)
    drive(agent, lambda: completed(stub) == 2, "both completed")
    assert ex.gauge.peak == 2


def test_slot_starts_next_attempt_while_previous_uploads(tmp_path: Path) -> None:
    agent, stub, ex = make(tmp_path, [slot("gpu0", ["GPU-a"])])
    stub.gate_uploads = True
    first, second = offer_for("gpu0"), offer_for("gpu0")
    stub.offers.append(first)
    open_all(ex, [first, second])
    drive(agent, lambda: stub.ev(stub.upload_started, first.attempt_id).is_set(), "first upload started")
    stub.offers.append(second)
    drive(agent, lambda: ex.ev(ex.entered, second.attempt_id).is_set(), "second attempt executing")
    assert completed(stub) == 0  # the first result is still uploading
    for o in (first, second):
        stub.ev(stub.upload_gates, o.attempt_id).set()
    drive(agent, lambda: completed(stub) == 2, "both delivered")


def test_slots_sharing_a_device_never_overlap(tmp_path: Path) -> None:
    agent, stub, ex = make(tmp_path, [slot("gpu0", ["GPU-a"]), slot("gpu1", ["GPU-b"])], shared_device="GPU-a")
    offers = [offer_for("gpu0"), offer_for("gpu1")]
    stub.offers.extend(offers)
    drive(agent, lambda: any(ex.ev(ex.entered, o.attempt_id).is_set() for o in offers), "first attempt running")
    time.sleep(0.1)
    for _ in range(5):
        agent.step()
    assert sum(ex.ev(ex.entered, o.attempt_id).is_set() for o in offers) == 1
    open_all(ex, offers)
    drive(agent, lambda: completed(stub) == 2, "both completed")
    assert ex.gauge.peak == 1


def test_pushed_offer_for_busy_slot_is_rejected(tmp_path: Path) -> None:
    agent, stub, ex = make(tmp_path, [slot("gpu0", ["GPU-a"])])
    first, second = offer_for("gpu0"), offer_for("gpu0")
    stub.offers.append(first)
    drive(agent, lambda: ex.ev(ex.entered, first.attempt_id).is_set(), "first running")
    agent.handle_offer(second)
    assert [a[0] for a in stub.names("reject")] == [second.attempt_id]
    assert stub.names("reject")[0][1].reason == "admission_busy"
    assert second.attempt_id not in [a for a in stub.names("accept")]
    open_all(ex, [first])
    drive(agent, lambda: completed(stub) == 1, "first completed")


def test_acquire_advertises_only_idle_slots(tmp_path: Path) -> None:
    agent, stub, ex = make(tmp_path, [slot("gpu0", ["GPU-a"]), slot("gpu1", ["GPU-b"])])
    busy = offer_for("gpu0")
    stub.offers.append(busy)
    assert stub.free_history == []
    drive(agent, lambda: ex.ev(ex.entered, busy.attempt_id).is_set(), "gpu0 running")
    mark = len(stub.free_history)
    for _ in range(4):
        agent.step()
        time.sleep(0.01)
    assert stub.free_history[0] == ["gpu0", "gpu1"]
    assert stub.free_history[mark:] and all(f == ["gpu1"] for f in stub.free_history[mark:])
    open_all(ex, [busy])
    drive(agent, lambda: completed(stub) == 1, "completed")
    wait_for(lambda: not agent._sup.any_busy(), "slot released")
    agent.step()
    assert stub.free_history[-1] == ["gpu0", "gpu1"]


def test_barrier_recheck_deferred_while_any_slot_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, stub, ex = make(tmp_path, [slot("gpu0", ["GPU-a"]), slot("gpu1", ["GPU-b"])])
    calls: list[set[str] | None] = []

    def fake_recover(config: Any, executor: Any, state: Any, only: set[str] | None = None) -> dict[str, str]:
        calls.append(only)
        return {s: "ready" for s in (only or {"gpu0", "gpu1"})}

    monkeypatch.setattr(agent_mod, "recover_slots", fake_recover)
    agent._slot_states["gpu1"] = "unknown"
    busy = offer_for("gpu0")
    stub.offers.append(busy)
    drive(agent, lambda: ex.ev(ex.entered, busy.attempt_id).is_set(), "gpu0 running")
    agent.clock.t = 1000.0  # type: ignore[attr-defined]
    for _ in range(3):
        agent.step()
    assert calls == []
    open_all(ex, [busy])
    drive(agent, lambda: calls != [], "barrier rechecked once idle")
    assert calls == [{"gpu1"}]


def test_transfer_pool_is_bounded(tmp_path: Path) -> None:
    slots = [slot(f"gpu{i}", [f"GPU-{i}"]) for i in range(4)]
    agent, stub, ex = make(tmp_path, slots, transfer_workers=2)
    stub.gate_uploads = True
    offers = [offer_for(f"gpu{i}") for i in range(4)]
    stub.offers.extend(offers)
    open_all(ex, offers)
    drive(agent, lambda: stub.upload_gauge.now == 2, "two uploads in flight")
    for _ in range(10):
        agent.step()
        time.sleep(0.01)
    assert stub.upload_gauge.now == 2 and stub.upload_gauge.peak == 2
    assert not agent._sup.any_busy()  # all four slots are free: only the uploads are queued
    for o in offers:
        stub.ev(stub.upload_gates, o.attempt_id).set()
    drive(agent, lambda: completed(stub) == 4, "all delivered")
    assert stub.upload_gauge.peak == 2


def test_stop_waits_for_workers(tmp_path: Path) -> None:
    agent, stub, ex = make(tmp_path, [slot("gpu0", ["GPU-a"])], idle_s=0.01)
    o = offer_for("gpu0")
    stub.offers.append(o)
    stop = threading.Event()
    t = threading.Thread(target=agent.run_forever, args=(stop,))
    t.start()
    wait_for(lambda: ex.ev(ex.entered, o.attempt_id).is_set(), "attempt running")
    stop.set()
    t.join(0.3)
    assert t.is_alive()  # still waiting for the running attempt
    open_all(ex, [o])
    t.join(WAIT_S)
    assert not t.is_alive()
    assert completed(stub) == 1


def test_unfinished_attempt_stays_recoverable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_mod, "_SHUTDOWN_WAIT_S", 0.2)
    agent, stub, ex = make(tmp_path, [slot("gpu0", ["GPU-a"])], idle_s=0.01)
    o = offer_for("gpu0")
    stub.offers.append(o)
    stop = threading.Event()
    t = threading.Thread(target=agent.run_forever, args=(stop,))
    t.start()
    wait_for(lambda: ex.ev(ex.entered, o.attempt_id).is_set(), "attempt running")
    stop.set()
    t.join(WAIT_S)
    assert not t.is_alive()
    row = agent.state.get_attempt(o.attempt_id)
    assert row is not None and row.state == "executing"
    assert agent._mark_crashed_executions() == [o.attempt_id]  # restart recovery resolves it
    open_all(ex, [o])  # let the orphaned worker thread finish
    wait_for(lambda: not agent._sup.any_busy(), "worker ended")
