"""Node-mode scheduling: a bounded pool of continuation workers per capability class (R8), sized from the eligible
slots of fresh runners. In-process simulated runners; contract evidence, never GPU proof."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from assetstudio_server.coordinator.runner import Coordinator
from assetstudio_server.main import create_app
from assetstudio_server.studio import build_studio
from fastapi.testclient import TestClient

from tests.conftest import Api, make_settings
from tests.contract.test_api_batches import confirm_all, create, detail, setup_project
from tests.contract.test_node_execution import Runner, runner_factory, wait_for  # noqa: F401 - fixture

CALL_S = 0.4


@pytest.fixture
def node_api(tmp_path: Path) -> Iterator[Api]:
    settings = make_settings(tmp_path)
    settings.execution = "nodes"
    settings.runner_offer_ttl_s = 5
    settings.runner_maintenance_s = 0.2
    studio = build_studio(settings)
    client = TestClient(create_app(settings, studio))
    client.__enter__()
    yield Api(client, studio)
    client.__exit__(None, None, None)


class Timed:
    """Wraps an engine: every call sleeps and records its (start, end) window for overlap assertions."""

    def __init__(self, inner: Any, windows: list[tuple[float, float]]) -> None:
        self._inner, self._windows = inner, windows

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr) or name in ("health", "lease", "check"):
            return attr

        def call(*a: Any, **kw: Any) -> Any:
            start = time.monotonic()
            time.sleep(CALL_S)
            try:
                return attr(*a, **kw)
            finally:
                self._windows.append((start, time.monotonic()))
        return call


def coordinator(api: Api) -> Coordinator:
    return api.c.app.state.coordinator  # type: ignore[no-any-return]


def test_pool_grows_with_the_eligible_slots(node_api: Api, runner_factory: Callable[..., Runner]) -> None:  # noqa: F811
    coord = coordinator(node_api)
    runner_factory(name="a")
    wait_for(lambda: coord._pool.target("gpu1") == 1)
    assert coord._pool.keys("gpu1") == ["gpu1#0"] and coord._pool.keys("gpu0") == ["gpu0#0"]
    runner_factory(name="b")
    wait_for(lambda: coord._pool.target("gpu1") == 2 and len(coord._pool.keys("gpu1")) == 2)
    wait_for(lambda: len(coord._pool.keys("gpu0")) == 2)
    lanes = coord.status()["lanes"]["gpu1"]
    assert set(lanes["workers"]) == {"gpu1#0", "gpu1#1"} and lanes["pool_target"] == 2


def test_pool_is_capped_by_the_setting(node_api: Api, runner_factory: Callable[..., Runner]) -> None:  # noqa: F811
    node_api.studio.settings.node_workers_max = 1
    coord = coordinator(node_api)
    runner_factory(name="a")
    runner_factory(name="b")
    time.sleep(0.8)
    assert coord._pool.target("gpu1") == 1 and coord._pool.keys("gpu1") == ["gpu1#0"]


def test_two_runners_work_in_parallel(node_api: Api, runner_factory: Callable[..., Runner]) -> None:  # noqa: F811
    api = node_api
    windows: dict[str, list[tuple[float, float]]] = {"a": [], "b": []}
    runners = {n: runner_factory(name=n) for n in windows}
    for n, r in runners.items():
        r.engines.aux = Timed(r.engines.aux, windows[n])
    coord = coordinator(api)
    wait_for(lambda: len(coord._pool.keys("gpu1")) == 2)
    pid = setup_project(api)
    create(api, pid, ["one", "two", "three", "four"], "sched-parallel", candidate_count=1)
    api.wait_ops(timeout=60)
    enhance = [a for a in api.studio.journal.attempts.list() if a["operation"] == "aux.enhance"]
    assert len(enhance) == 4 and len({a["runner_id"] for a in enhance}) == 2
    assert windows["a"] and windows["b"]
    assert any(s1 < e2 and s2 < e1 for s1, e1 in windows["a"] for s2, e2 in windows["b"]), windows
    passes = {p["lane"] for p in api.studio.journal.tasks.passes(limit=50)}
    assert {"gpu1#0", "gpu1#1"} <= passes


def test_consecutive_calls_of_one_task_keep_their_slot(node_api: Api, runner_factory: Callable[..., Runner]) -> None:  # noqa: F811
    api = node_api
    runner_factory(name="a")
    runner_factory(name="b")
    pid = setup_project(api)
    bid = create(api, pid, ["Tavern"], "sched-pref")["batch"]["id"]
    api.wait_ops()
    confirm_all(api, pid, bid, "sched-pref-confirm")
    api.wait_ops()
    assert detail(api, pid, bid)["items"][0]["candidate_set"] is not None
    t2i = [a for a in api.studio.journal.attempts.list() if a["operation"] == "image.t2i"]
    assert len(t2i) == 4
    for task_id in {a["task_id"] for a in t2i}:
        placed = {(a["runner_id"], a["slot_id"]) for a in t2i if a["task_id"] == task_id}
        assert len(placed) == 1, placed


def test_a_stale_runner_leaves_the_target_without_thread_churn(
        node_api: Api, runner_factory: Callable[..., Runner]) -> None:  # noqa: F811
    api, coord = node_api, coordinator(node_api)
    runner_factory(name="a")
    b = runner_factory(name="b")
    wait_for(lambda: coord._pool.target("gpu1") == 2 and len(coord._pool.keys("gpu1")) == 2)
    before = {t.name for t in threading.enumerate()}
    api.studio.settings.runner_lease_s = 2  # the session contract floors it at 15: shrink after registering
    b.stop()  # no more heartbeats: its session goes stale after runner_lease_s
    wait_for(lambda: coord._pool.target("gpu1") == 1, timeout=10)
    wait_for(lambda: coord._pool.keys("gpu1") == ["gpu1#0"], timeout=10)
    assert {t.name for t in threading.enumerate() if t.name.startswith("lane-")} <= before
    pid = setup_project(api)
    create(api, pid, ["still works"], "sched-stale-1", candidate_count=1)
    api.wait_ops()
    enhance = [a for a in api.studio.journal.attempts.list() if a["operation"] == "aux.enhance"]
    assert len(enhance) == 1 and enhance[0]["state"] in ("ingested", "committed")
