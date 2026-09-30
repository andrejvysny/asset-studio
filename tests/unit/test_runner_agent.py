"""Compute-runner agent against an in-memory stub client (no server, no engines)."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any, BinaryIO

import httpx
import pytest
from assetstudio_client import ApiError, TransportError, keys
from assetstudio_core.ids import new_id
from assetstudio_node.agent import RunnerAgent
from assetstudio_node.cli import main as cli_main
from assetstudio_node.config import ConfigError, RunnerConfig, load_config
from assetstudio_node.engine_executor import EngineExecutor, Engines
from assetstudio_node.engines.fake import FakeAux, FakeWorker3d
from assetstudio_node.executor import ExecutionBlocked, FakeExecutor
from assetstudio_node.hostlock import HostLock, HostLockBusy
from assetstudio_node.inventory import build_inventory
from assetstudio_node.push import PushListener
from assetstudio_node.spool import Spool
from assetstudio_node.state import RunnerState
from assetstudio_protocol.engine import AckError
from assetstudio_protocol.execution import (
    AcceptResponse,
    DispositionReceipt,
    InputRef,
    Offer,
    Policy,
    ReportAck,
    Requirements,
    compute_input_digest,
)
from assetstudio_protocol.runners import (
    Control,
    HeartbeatResponse,
    RegisterResponse,
    SessionAccepted,
    StudioKey,
)
from pydantic import ValidationError

RID = new_id("rnr")
GID = new_id("rgp")
SID = new_id("rse")
INPUT = b"abc"
INPUT_SHA = hashlib.sha256(INPUT).hexdigest()
STUDIO_PRIV = keys.generate_private_key()
STUDIO_KEY = StudioKey(key_id="k1", public_key=keys.public_key_b64(STUDIO_PRIV), status="current")
TOKEN = "t" * 24


def slot(slot_id: str = "gpu0", devices: list[str] | None = None, capability: str = "image",
         engines: list[str] | None = None) -> dict[str, Any]:
    return {"slot_id": slot_id, "capability": capability, "devices": devices or ["GPU-aaa"],
            "engines": engines or ["comfyui"]}


def cfg_dict(tmp_path: Path, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"studio_url": "http://studio.test", "name": "r1", "state_dir": str(tmp_path / "state"),
                            "host_lock": str(tmp_path / "host.lock"), "slots": [slot()], "simulated": True}
    return {**base, **over}


def make_offer(aid: str | None = None, operation: str = "image.t2i", params: dict[str, Any] | None = None,
               signed: bool = False) -> Offer:
    inputs = [InputRef(sha256=INPUT_SHA, size=len(INPUT), role="source", mime="image/png")]
    params = params or {"prompt": "x"}
    req = Requirements(capability="image", engine="comfyui")
    offer = Offer(schema="assetstudio.execution.v1", attempt_id=aid or new_id("atp"), task_id="t", call_key="j/1",
                  generation=1, runner_id=RID, session_id=SID, slot_id="gpu0", operation=operation,
                  operation_version=1, input_digest=compute_input_digest(operation, 1, inputs, params, req, Policy()),
                  inputs=inputs, params=params, requirements=req, offer_expires_at="2026-01-01T00:00:00Z")
    return keys.sign_offer(STUDIO_PRIV, offer) if signed else offer


def empty_hb() -> HeartbeatResponse:
    return HeartbeatResponse(lease_until="2026-01-01T00:00:00Z")


def receipt_hb(offer: Offer) -> HeartbeatResponse:
    r = DispositionReceipt(attempt_id=offer.attempt_id, generation=offer.generation, disposition="committed",
                           at="2026-01-01T00:00:00Z")
    return HeartbeatResponse(lease_until="2026-01-01T00:00:00Z", receipts=[r])


class StubClient:
    def __init__(self, *, ephemeral: bool = False) -> None:
        self.runner_id: str | None = None
        self.ephemeral = ephemeral
        self.calls: list[tuple[str, Any]] = []
        self.offers: deque[Offer] = deque()
        self.heartbeats: deque[HeartbeatResponse] = deque()
        self.start_authorized = True
        self.upload_failures = 0
        self.receipt_result: DispositionReceipt | None = None
        self.hello: Any = None
        self.deregistered = False
        self.catalog: dict[str, Any] = {}

    def names(self, name: str) -> list[Any]:
        return [a for n, a in self.calls if n == name]

    def register(self, req: Any) -> RegisterResponse:
        self.calls.append(("register", req))
        return RegisterResponse(runner_id=RID, group_id=GID, ephemeral=self.ephemeral, studio_keys=[STUDIO_KEY])

    def open_session(self, hello: Any) -> SessionAccepted:
        self.hello = hello
        self.calls.append(("open_session", hello))
        return SessionAccepted(session_id=SID, protocol_version=1, catalog_sha256="c" * 64, catalog=self.catalog,
                               studio_keys=[STUDIO_KEY], heartbeat_s=10, lease_s=60, offer_ttl_s=30)

    def put_inventory(self, sid: str, inv: Any) -> None:
        self.calls.append(("put_inventory", inv))

    def heartbeat(self, sid: str, hb: Any) -> HeartbeatResponse:
        self.calls.append(("heartbeat", hb))
        return self.heartbeats.popleft() if self.heartbeats else empty_hb()

    def acquire(self, sid: str, req: Any) -> Offer | None:
        self.calls.append(("acquire", req))
        return self.offers.popleft() if self.offers else None

    def accept(self, aid: str, req: Any) -> AcceptResponse:
        self.calls.append(("accept", aid))
        return AcceptResponse(attempt_id=aid, generation=req.generation, start_authorized=self.start_authorized,
                              lease_until="2026-01-01T00:00:00Z")

    def report(self, aid: str, req: Any) -> ReportAck:
        self.calls.append(("report", req.report))
        return ReportAck(state=req.report.state)

    def download_input(self, aid: str, sha: str, dst: BinaryIO, *, start: int = 0) -> int:
        self.calls.append(("download_input", sha))
        dst.write(INPUT)
        return len(INPUT)

    def upload_file(self, path: Path, *, attempt_id: str, generation: int, role: str, mime: str) -> None:
        if self.upload_failures:
            self.upload_failures -= 1
            raise TransportError("boom")
        self.calls.append(("upload_file", (path.read_bytes(), attempt_id, generation, role, mime)))

    def complete(self, aid: str, req: Any) -> ReportAck:
        self.calls.append(("complete", req))
        return ReportAck(state="ingested")

    def receipt(self, aid: str) -> DispositionReceipt | None:
        self.calls.append(("receipt", aid))
        return self.receipt_result

    def deregister(self) -> None:
        self.deregistered = True
        self.calls.append(("deregister", None))

    def reject(self, *a: Any) -> None:
        self.calls.append(("reject", a))


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class TickClock(Clock):
    def __call__(self) -> float:
        self.t += 100
        return self.t


def make_agent(tmp_path: Path, stub: StubClient | None = None, *, executor: Any = None,
               clock: Clock | None = None, **cfg: Any) -> tuple[RunnerAgent, StubClient, Clock]:
    """`executor` may be an instance or a factory `(config, state) -> executor` (engine executors need the state)."""
    stub = stub or StubClient()
    clock = clock or Clock()
    config = RunnerConfig.model_validate(cfg_dict(tmp_path, **cfg))
    state = RunnerState(config.state_dir)
    if callable(executor):
        executor = executor(config, state)
    agent = RunnerAgent(config, client=stub, executor=executor or FakeExecutor(), state=state,
                        spool=Spool(config.state_dir / "spool"), clock=clock, idle_s=0.0)  # type: ignore[arg-type]
    agent.bootstrap(TOKEN)
    agent.open_session()
    return agent, stub, clock


def reported_states(stub: StubClient) -> list[str]:
    return [r.state for r in stub.names("report")]


# -- config ---------------------------------------------------------------------------------------------------------


def test_config_rejects_bad_slot_maps(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="overlap"):
        RunnerConfig.model_validate(cfg_dict(tmp_path, slots=[slot("a"), slot("b")]))
    with pytest.raises(ValidationError, match="unique"):
        RunnerConfig.model_validate(cfg_dict(tmp_path, slots=[slot("a"), slot("a", ["GPU-bbb"])]))
    with pytest.raises(ValidationError, match="may only run comfyui"):
        RunnerConfig.model_validate(cfg_dict(tmp_path, slots=[slot(engines=["aux"])]))
    with pytest.raises(ValidationError, match="aux/worker3d"):
        RunnerConfig.model_validate(cfg_dict(tmp_path, slots=[slot(capability="aux3d", engines=["comfyui"])]))
    with pytest.raises(ValidationError, match="push_listen"):
        RunnerConfig.model_validate(cfg_dict(tmp_path, dispatch="push"))
    with pytest.raises(ValidationError):
        RunnerConfig.model_validate(cfg_dict(tmp_path, bogus=1))
    ok = RunnerConfig.model_validate(cfg_dict(tmp_path, dispatch="push", push_listen="127.0.0.1:9000"))
    assert ok.key_path == ok.state_dir / "runner.key"


def test_inventory_from_config(tmp_path: Path) -> None:
    config = RunnerConfig.model_validate(cfg_dict(
        tmp_path, slots=[slot("a", ["GPU-aaa"]), slot("b", ["index:1"], "aux3d", ["aux", "worker3d"])]))
    inv = build_inventory(config, 3, "c" * 64, runner_id=RID)
    assert inv.revision == 3
    assert [d.uuid for d in inv.devices] == ["GPU-aaa", f"{RID}/1"]
    assert inv.devices[1].fallback and inv.devices[1].index == 1
    assert {o.op for e in inv.slots[1].engines for o in e.operations} >= {"aux.qa", "worker3d.export"}
    assert all(s.state == "ready" for s in inv.slots)


def test_inventory_resolves_devices_from_nvidia_smi(tmp_path: Path) -> None:
    gpus = [{"index": "0", "uuid": "GPU-real0", "name": "RTX", "vram_total_mb": 24000},
            {"index": "1", "uuid": "GPU-real1", "name": "RTX", "vram_total_mb": 12000}]
    config = RunnerConfig.model_validate(cfg_dict(tmp_path, slots=[slot("a", ["index:0"]), slot("b", ["GPU-real1"])]))
    inv = build_inventory(config, 1, "c" * 64, runner_id=RID, gpus=gpus)
    assert [(d.uuid, d.index, d.name, d.memory_mb, d.fallback) for d in inv.devices] == [
        ("GPU-real0", 0, "RTX", 24000, False), ("GPU-real1", 1, "RTX", 12000, False)]
    assert [s.device_uuids for s in inv.slots] == [["GPU-real0"], ["GPU-real1"]]
    for slots in ([slot("a", ["GPU-nope"])], [slot("a", ["index:7"])], [slot("a", ["index:0"]), slot("b", ["GPU-real0"])]):
        bad = RunnerConfig.model_validate(cfg_dict(tmp_path, slots=slots))
        with pytest.raises(ConfigError):
            build_inventory(bad, 1, "c" * 64, runner_id=RID, gpus=gpus)


# -- host lock, state, spool ----------------------------------------------------------------------------------------


def test_host_lock_exclusive_and_released(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "host.lock"
    with HostLock(path):
        with pytest.raises(HostLockBusy, match="host.lock"), HostLock(path):
            pass
        code = ("import sys; from pathlib import Path; from assetstudio_node.hostlock import HostLock, HostLockBusy\n"
                "try:\n    HostLock(Path(sys.argv[1])).__enter__()\nexcept HostLockBusy:\n    sys.exit(7)\n")
        assert subprocess.run([sys.executable, "-c", code, str(path)], check=False).returncode == 7
    with HostLock(path):
        assert path.read_text().strip().isdigit()


def test_state_record_attempt_is_insert_if_absent(tmp_path: Path) -> None:
    state = RunnerState(tmp_path / "s")
    offer = make_offer()
    assert state.record_attempt(offer) is True
    assert state.record_attempt(offer) is False
    row = state.get_attempt(offer.attempt_id)
    assert row is not None and row.state == "admitted" and row.offer == offer
    state.set_state(offer.attempt_id, "failed", error="x")
    assert state.get_attempt(offer.attempt_id).error == "x"  # type: ignore[union-attr]
    state.delete_attempt(offer.attempt_id)
    assert state.list_attempts() == []
    state.set_identity("k", "v1")
    state.set_identity("k", "v2")
    assert state.get_identity("k") == "v2" and state.get_identity("missing") is None


def test_spool_atomic_write_manifest_delete(tmp_path: Path) -> None:
    spool = Spool(tmp_path / "spool")
    aid = new_id("atp")
    ref = spool.write_file(aid, "a.bin", b"hello", "application/octet-stream")
    src = tmp_path / "src.bin"
    src.write_bytes(b"world")
    ref2 = spool.write_file(aid, "b.bin", src, "application/octet-stream")
    assert ref.sha256 == hashlib.sha256(b"hello").hexdigest() and ref.size == 5 and ref2.size == 5
    assert spool.manifest(aid) is None and spool.unsynced_bytes() == 10
    assert not [p for p in spool.dir_for(aid).iterdir() if p.name.endswith(".tmp")]
    m = spool.write_manifest(aid, 1, [ref, ref2], {"k": 1})
    assert spool.manifest(aid) == m and spool.unsynced_bytes() == 0
    assert [p.name for p in spool.files(aid)] == ["a.bin", "b.bin"]
    assert spool.attempt_ids() == [aid]
    spool.delete(aid)
    assert spool.attempt_ids() == [] and spool.manifest(aid) is None


# -- agent ----------------------------------------------------------------------------------------------------------


def test_bootstrap_registers_once_and_persists_identity(tmp_path: Path) -> None:
    agent, stub, _ = make_agent(tmp_path)
    assert len(stub.names("register")) == 1 and stub.runner_id == RID
    assert agent.state.get_identity("runner_id") == RID and agent.state.get_identity("group_id") == GID
    agent2, stub2, _ = make_agent(tmp_path)  # same state dir: no token needed, no second registration
    assert stub2.names("register") == [] and stub2.runner_id == RID
    assert agent2.studio_keys == [STUDIO_KEY]


def test_happy_path_spools_uploads_and_receipt_releases(tmp_path: Path) -> None:
    executor = FakeExecutor()
    agent, stub, clock = make_agent(tmp_path, executor=executor)
    offer = make_offer()
    stub.offers.append(offer)
    assert agent.step() is True
    aid = offer.attempt_id
    assert executor.calls == 1
    assert reported_states(stub) == ["admitted", "executing", "spooled", "uploading"]
    ((content, up_aid, gen, role, mime),) = stub.names("upload_file")
    assert content.startswith(b"SIMULATED:") and (up_aid, gen, role) == (aid, 1, "result.bin")
    (complete,) = stub.names("complete")
    assert complete.manifest.files[0].sha256 == hashlib.sha256(content).hexdigest()
    assert complete.manifest.meta == {"simulated": True, "operation": "image.t2i"}
    assert agent.state.get_attempt(aid).state == "ingested"  # type: ignore[union-attr]
    assert stub.names("download_input") == [INPUT_SHA]
    assert spool_has(agent, aid)
    order = [n for n, _ in stub.calls]
    assert order.index("accept") < order.index("report") < order.index("upload_file") < order.index("complete")

    stub.heartbeats.append(receipt_hb(offer))
    clock.t += 100
    agent.step()
    assert agent.state.list_attempts() == [] and agent.spool.attempt_ids() == []
    hb_with_attempt = [h for h in stub.names("heartbeat") if h.attempts]
    assert hb_with_attempt and hb_with_attempt[0].attempts[0].state == "ingested"


def spool_has(agent: RunnerAgent, aid: str) -> bool:
    return agent.spool.manifest(aid) is not None and bool(agent.spool.files(aid))


def test_fake_executor_is_deterministic(tmp_path: Path) -> None:
    ex = FakeExecutor()
    offer = make_offer()
    outs = [ex.execute(offer, {}, tmp_path / str(i), lambda: False)[0][0][1].read_bytes() for i in range(2)]
    other = ex.execute(make_offer(params={"prompt": "y"}), {}, tmp_path / "z", lambda: False)[0][0][1].read_bytes()
    assert outs[0] == outs[1] != other


def test_no_execution_without_start_authorized(tmp_path: Path) -> None:
    executor = FakeExecutor()
    agent, stub, _ = make_agent(tmp_path, executor=executor)
    stub.start_authorized = False
    stub.offers.append(make_offer())
    agent.step()
    assert executor.calls == 0 and agent.state.list_attempts() == [] and stub.names("report") == []


def test_duplicate_offer_executes_once(tmp_path: Path) -> None:
    executor = FakeExecutor()
    agent, stub, clock = make_agent(tmp_path, executor=executor)
    offer = make_offer()
    stub.offers.extend([offer, offer])
    agent.step()
    clock.t += 100
    agent.step()
    assert executor.calls == 1 and len(stub.names("accept")) == 2 and len(stub.names("complete")) == 1


def test_cancel_control_during_execution(tmp_path: Path) -> None:
    agent, stub, _ = make_agent(tmp_path, executor=FakeExecutor(delay_s=5.0), clock=TickClock())
    offer = make_offer()
    stub.offers.append(offer)
    stub.heartbeats.append(empty_hb())  # the step's own heartbeat
    stub.heartbeats.append(HeartbeatResponse(lease_until="2026-01-01T00:00:00Z", controls=[
        Control(attempt_id=offer.attempt_id, generation=1, control="cancel")]))
    agent.step()
    assert reported_states(stub) == ["admitted", "executing", "cancelled"]
    assert stub.names("upload_file") == [] and agent.state.list_attempts() == []


def test_execution_failure_maps_error_code(tmp_path: Path) -> None:
    cases = {"input_invalid": "invalid_input", "oom": "resource_exhausted", "internal": "uncertain_execution"}
    for i, (code, mapped) in enumerate(cases.items()):
        agent, stub, _ = make_agent(tmp_path / str(i), executor=FakeExecutor(fail_ops={"image.t2i": code}))  # type: ignore[arg-type]
        stub.offers.append(make_offer())
        agent.step()
        last = stub.names("report")[-1]
        assert last.state == "failed" and last.error.code == mapped
        assert stub.names("complete") == [] and agent.state.list_attempts() == []


def test_transport_error_during_upload_is_resumed(tmp_path: Path) -> None:
    agent, stub, clock = make_agent(tmp_path)
    offer = make_offer()
    stub.offers.append(offer)
    stub.upload_failures = 1
    agent.step()
    assert stub.names("complete") == []
    assert agent.state.get_attempt(offer.attempt_id).state == "uploading"  # type: ignore[union-attr]
    clock.t += 100
    agent.step()
    assert len(stub.names("upload_file")) == 1 and len(stub.names("complete")) == 1
    assert agent.state.get_attempt(offer.attempt_id).state == "ingested"  # type: ignore[union-attr]


def test_lost_receipt_is_fetched_with_get(tmp_path: Path) -> None:
    agent, stub, clock = make_agent(tmp_path)
    offer = make_offer()
    stub.offers.append(offer)
    agent.step()
    stub.receipt_result = DispositionReceipt(attempt_id=offer.attempt_id, generation=1, disposition="committed",
                                             at="2026-01-01T00:00:00Z")
    clock.t += 100
    agent.step()
    assert agent.state.list_attempts() == [] and agent.spool.attempt_ids() == []


def test_stale_generation_stops_work_and_keeps_spool(tmp_path: Path) -> None:
    agent, stub, _ = make_agent(tmp_path)
    offer = make_offer()
    stub.offers.append(offer)

    def stale(aid: str, req: Any) -> Any:
        raise ApiError(409, "stale_generation", "superseded")

    stub.complete = stale  # type: ignore[method-assign]
    agent.step()
    row = agent.state.get_attempt(offer.attempt_id)
    assert row is not None and row.state == "quarantined" and agent.spool.files(offer.attempt_id)


def test_restart_reports_local_attempts_with_spooled_files(tmp_path: Path) -> None:
    agent, stub, _ = make_agent(tmp_path)
    offer = make_offer()
    stub.offers.append(offer)
    stub.upload_failures = 1
    agent.step()
    agent.state.close()
    _, stub2, _ = make_agent(tmp_path)
    (local,) = stub2.hello.local_attempts
    assert local.attempt_id == offer.attempt_id and local.state == "uploading"
    assert [f.name for f in local.spooled] == ["result.bin"] and local.spooled[0].size > 0


def test_restart_reports_crashed_execution_as_lost(tmp_path: Path) -> None:
    """A09: an attempt left `executing` without a spooled manifest died with the agent: reported lost, then dropped."""
    agent, _, _ = make_agent(tmp_path)
    offer = make_offer()
    agent.state.record_attempt(offer)
    agent.state.set_state(offer.attempt_id, "executing")
    agent.state.close()
    agent2, stub2, _ = make_agent(tmp_path)
    (local,) = stub2.hello.local_attempts
    assert local.attempt_id == offer.attempt_id and local.state == "lost"
    assert agent2.state.get_attempt(offer.attempt_id) is None


def test_restart_finishes_a_spool_written_before_the_crash(tmp_path: Path) -> None:
    """A crash between the manifest write and the `spooled` step: the attempt is delivered, not stuck or lost."""
    agent, _, _ = make_agent(tmp_path)
    offer = make_offer()
    agent.state.record_attempt(offer)
    out = tmp_path / "out.bin"
    out.write_bytes(b"x" * 10)
    f = agent.spool.write_file(offer.attempt_id, "result.bin", out, "application/octet-stream")
    agent.spool.write_manifest(offer.attempt_id, offer.generation, [f], {})
    agent.state.set_state(offer.attempt_id, "executing")
    agent.state.close()
    agent2, stub2, _ = make_agent(tmp_path)
    (local,) = stub2.hello.local_attempts
    assert local.state == "spooled" and [x.name for x in local.spooled] == ["result.bin"]
    agent2.step()
    assert len(stub2.names("complete")) == 1


def test_pushed_offer_signature_is_enforced(tmp_path: Path) -> None:
    executor = FakeExecutor()
    agent, stub, _ = make_agent(tmp_path, executor=executor, dispatch="push", push_listen="127.0.0.1:0")
    agent.handle_offer(make_offer(), pushed=True)
    assert stub.names("accept") == [] and executor.calls == 0
    forged = make_offer(signed=True).model_copy(update={"signature": keys.sign(keys.generate_private_key(), b"x")})
    agent.handle_offer(forged, pushed=True)
    assert stub.names("accept") == []
    good = make_offer(signed=True)
    assert agent.enqueue_offer(good) is True and agent.enqueue_offer(good) is False
    assert agent.step() is True
    assert executor.calls == 1 and len(stub.names("complete")) == 1
    assert stub.names("acquire") == []  # push dispatch never polls


def test_push_listener_accepts_signed_rejects_unsigned(tmp_path: Path) -> None:
    agent, _, _ = make_agent(tmp_path, dispatch="push", push_listen="127.0.0.1:0")
    listener = PushListener("127.0.0.1:0", agent)
    listener.start()
    try:
        url = f"http://127.0.0.1:{listener.port}/v1/offers"
        good = make_offer(signed=True)
        assert httpx.post(url, content=good.model_dump_json()).status_code == 202
        assert httpx.post(url, content=good.model_dump_json()).status_code == 200  # duplicate while queued
        assert httpx.post(url, content=make_offer().model_dump_json()).status_code == 403
        assert httpx.post(url, content=b"{not json").status_code == 400
        assert httpx.post(url.replace("/v1/offers", "/other"), content=b"{}").status_code == 404
        assert httpx.get(url).status_code == 501
    finally:
        listener.stop()
    assert agent._pushed.qsize() == 1


def test_ephemeral_runner_deregisters_after_receipt(tmp_path: Path) -> None:
    agent, stub, clock = make_agent(tmp_path, StubClient(ephemeral=True))
    first, second = make_offer(), make_offer()
    stub.offers.extend([first, second])
    agent.step()
    assert len(stub.names("complete")) == 1 and not stub.deregistered
    stub.heartbeats.append(receipt_hb(first))
    clock.t += 100
    assert agent.step() is True
    assert stub.deregistered and agent.stopped
    assert stub.names("heartbeat")[-1].lifecycle == "safe_to_terminate"
    assert len(stub.names("accept")) == 1  # the second offer was never taken


def test_run_forever_survives_transport_errors(tmp_path: Path) -> None:
    agent, stub, _ = make_agent(tmp_path)
    stop = threading.Event()
    calls = {"n": 0}

    def flaky(sid: str, req: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransportError("down")
        stop.set()

    stub.acquire = flaky  # type: ignore[method-assign]
    agent._idle_s = 0.0
    t = threading.Thread(target=agent.run_forever, args=(stop,))
    t.start()
    t.join(timeout=10)
    assert not t.is_alive() and calls["n"] >= 2


# -- cli ------------------------------------------------------------------------------------------------------------


def test_cli_check_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = tmp_path / "good.yaml"
    good.write_text(
        f"studio_url: http://studio.test\nname: r1\nstate_dir: {tmp_path}/state\nslots:\n"
        "  - {slot_id: gpu0, capability: image, devices: [GPU-aaa], engines: [comfyui]}\n")
    assert cli_main(["check-config", "--config", str(good)]) == 0
    assert "ok" in capsys.readouterr().out
    bad = tmp_path / "bad.yaml"
    bad.write_text(good.read_text() + "  - {slot_id: gpu1, capability: image, devices: [GPU-aaa], engines: [comfyui]}\n")
    assert cli_main(["check-config", "--config", str(bad)]) == 1
    assert "overlap" in capsys.readouterr().err
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.yaml")
    assert cli_main(["run", "--config", str(good)]) == 2  # real engines need models_root


# -- engine executor wiring (R7 barrier, R8 spooled hook, R10 receipts) ---------------------------------------------

AUX_SLOT = [slot("gpu1", ["GPU-bbb"], "aux3d", ["aux", "worker3d"])]


class BlockingExecutor(FakeExecutor):
    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code

    def execute(self, *a: Any, **k: Any) -> Any:
        raise ExecutionBlocked(self.code, "engine said no")  # type: ignore[arg-type]


def test_blocked_execution_reports_failed_with_resource_code(tmp_path: Path) -> None:
    agent, stub, _ = make_agent(tmp_path / "a", executor=BlockingExecutor("node_unavailable"))
    stub.offers.append(make_offer())
    agent.step()
    last = stub.names("report")[-1]
    assert last.state == "failed" and last.error.code == "node_unavailable"
    assert len(stub.names("put_inventory")) == 1 and agent.state.list_attempts() == []  # slot stays in rotation

    agent, stub, _ = make_agent(tmp_path / "b", executor=BlockingExecutor("admission_rejected"))
    stub.offers.append(make_offer())
    agent.step()
    assert stub.names("report")[-1].error.code == "admission_rejected"
    inv = stub.names("put_inventory")[-1]
    assert inv.revision == 2 and [s.state for s in inv.slots] == ["unknown"]  # out of rotation until the barrier passes
    before = len(stub.names("acquire"))
    agent.step()
    assert len(stub.names("acquire")) == before  # no schedulable slot: no long poll


def test_spooled_hook_runs_after_manifest_before_spooled_report(tmp_path: Path) -> None:
    seen: list[Any] = []

    class Hooked(FakeExecutor):
        def spooled(self, offer: Any) -> None:
            seen.append((reported_states(stub), agent.spool.manifest(offer.attempt_id) is not None))

    agent, stub, _ = make_agent(tmp_path, executor=Hooked())
    stub.offers.append(make_offer())
    agent.step()
    assert seen == [(["admitted", "executing"], True)]
    assert reported_states(stub)[2:] == ["spooled", "uploading"]


def test_inventory_carries_barrier_states_receipts_and_rechecks(tmp_path: Path) -> None:
    stub = StubClient()
    stub.catalog = {"models": {"m1": {"repo": "o/m1", "revision": "r", "service": "aux", "files": {"f": {}}},
                               "m2": {"repo": "o/m2", "revision": "r", "service": "comfyui", "files": {}}}}
    aux = FakeAux()
    aux.unload_response = AckError("refuses to unload")
    agent, stub, clock = make_agent(
        tmp_path, stub, slots=AUX_SLOT, clock=Clock(),
        executor=lambda c, s: EngineExecutor(c, s, Engines(None, aux, FakeWorker3d()), sleep=lambda _s: None))
    (inv,) = stub.names("put_inventory")
    assert inv.revision == 1 and [(s.slot_id, s.state) for s in inv.slots] == [("gpu1", "unknown")]
    assert [(m.key, m.status) for m in inv.models] == [("m1", "ok")]  # only models of this runner's engines
    agent.step()
    assert stub.names("acquire") == [] and stub.names("heartbeat")[-1].inventory_revision == 1
    clock.t += 10  # inside the 30 s retry window: no barrier re-run
    agent.step()
    assert len(stub.names("put_inventory")) == 1
    aux.unload_response = None
    clock.t += 40
    agent.step()
    inv2 = stub.names("put_inventory")[-1]
    assert inv2.revision == 2 and [s.state for s in inv2.slots] == ["ready"]
    agent.step()
    assert stub.names("acquire")[-1].free_slots == ["gpu1"]


def test_cli_models_verify_prints_receipts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "models"
    (root / "m").mkdir(parents=True)
    (root / "m" / "w.bin").write_bytes(b"right")
    lock = tmp_path / "lock.yaml"
    lock.write_text(
        "schema_version: 1\nmodels:\n  ok:\n    repo: o/ok\n    revision: r1\n    local_dir: m\n    service: aux\n"
        f"    files:\n      w.bin: {{size: 5, sha256: {hashlib.sha256(b'right').hexdigest()}}}\n"
        "  gone:\n    repo: o/gone\n    revision: r1\n    local_dir: nope\n    service: aux\n"
        "    files:\n      w.bin: {size: 5, sha256: " + "0" * 64 + "}\n")
    cfg = tmp_path / "node.yaml"
    cfg.write_text(f"studio_url: http://studio.test\nname: r1\nstate_dir: {tmp_path}/state\nmodels_root: {root}\n"
                   "slots:\n  - {slot_id: gpu1, capability: aux3d, devices: [GPU-b], engines: [aux]}\n")
    assert cli_main(["models", "verify", "--config", str(cfg), "--catalog", str(lock)]) == 1  # one model missing
    rows = {r["key"]: r["status"] for r in map(json.loads, capsys.readouterr().out.splitlines())}
    assert rows == {"ok": "ok", "gone": "missing"}
