"""Shutdown coordination: mutation gate, ownership of the Studio, and fail-together companions."""
from __future__ import annotations

import asyncio
import threading
import time

import pytest
from assetstudio_server.lifecycle import GateClosed, MutationGate
from assetstudio_server.main import _guarded, create_app
from fastapi.testclient import TestClient

from tests.conftest import make_settings


def test_gate_counts_and_waits() -> None:
    g = MutationGate()
    done = threading.Event()

    def work() -> None:
        with g.enter():
            done.wait(5)

    t = threading.Thread(target=work)
    t.start()
    while g.active == 0:
        time.sleep(0.005)
    assert not g.close_and_wait(0.05)  # still active -> timeout
    done.set()
    assert g.close_and_wait(5)
    t.join()
    assert g.active == 0


def test_closed_gate_refuses() -> None:
    g = MutationGate()
    assert g.close_and_wait(1)
    with pytest.raises(GateClosed), g.enter():
        pass


def test_borrowed_studio_survives_lifespan(tmp_path) -> None:
    from assetstudio_server.studio import build_studio

    st = build_studio(make_settings(tmp_path, "fake", False))
    closed: list[int] = []
    st.close = lambda: closed.append(1)  # type: ignore[method-assign]
    with TestClient(create_app(st.settings, st, owns_studio=False)):
        pass
    assert closed == []
    with TestClient(create_app(st.settings, st)):
        pass
    assert closed == [1]


class _Fake:
    def __init__(self, fail: BaseException | None = None, run_until_exit: bool = True) -> None:
        self.should_exit, self.started, self.fail, self.until = False, False, fail, run_until_exit

    async def serve(self) -> None:
        if self.fail:
            raise self.fail
        self.started = True
        while self.until and not self.should_exit:
            await asyncio.sleep(0.005)


def _run(servers: list[_Fake]) -> list[str]:
    failures: list[str] = []

    async def go() -> None:
        await asyncio.wait_for(asyncio.gather(*(_guarded(f"s{i}", s, servers, failures)  # type: ignore[arg-type]
                                                for i, s in enumerate(servers))), 5)
    asyncio.run(go())
    return failures


def test_companion_startup_failure_stops_everything() -> None:
    main, ok, bad = _Fake(), _Fake(), _Fake(SystemExit(1))
    assert _run([main, ok, bad]) == ["s2"]
    assert main.should_exit and ok.should_exit


def test_companion_silent_startup_failure_is_a_failure() -> None:
    main, quiet = _Fake(), _Fake(run_until_exit=False)
    quiet.serve = lambda: asyncio.sleep(0)  # type: ignore[method-assign] - returns without starting
    assert _run([main, quiet]) == ["s1"]
    assert main.should_exit


def test_main_failure_stops_companions() -> None:
    main, comp = _Fake(RuntimeError("boom")), _Fake()
    assert _run([main, comp]) == ["s0"]
    assert comp.should_exit


def test_orderly_exit_is_not_a_failure() -> None:
    main, comp = _Fake(), _Fake()
    main.should_exit = True  # signal already forwarded
    assert _run([main, comp]) == []
