"""Studio restart in node mode (R8): tasks whose runner attempts are live reconcile, they are never re-placed.
The restart is in-process: the old Studio's journal is closed under its lane thread (a crash), a new Studio is built
over the same instance dir and swapped into the same app, so the running runner keeps talking to "the" Studio."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from assetstudio_server.coordinator.runner import Coordinator
from assetstudio_server.main import create_app
from assetstudio_server.services.runner_maintenance import RunnerMaintenance
from assetstudio_server.studio import build_studio
from fastapi import Request, Response
from fastapi.testclient import TestClient
from starlette.concurrency import run_in_threadpool

from tests.conftest import Api, make_settings
from tests.contract.test_api_batches import confirm_all, create, detail, setup_project
from tests.contract.test_node_execution import Runner, all_attempts, wait_for


class Gate:
    """While Studio is 'down', runner requests wait (a retrying client); closing drains requests already inside."""

    def __init__(self) -> None:
        self._cond, self._open, self._active = threading.Condition(), True, 0

    def _enter(self) -> None:
        with self._cond:
            self._cond.wait_for(lambda: self._open, timeout=60)
            self._active += 1

    def _leave(self) -> None:
        with self._cond:
            self._active -= 1
            self._cond.notify_all()

    async def __call__(self, request: Request, call_next: Any) -> Response:
        await run_in_threadpool(self._enter)
        try:
            return await call_next(request)
        finally:
            self._leave()

    def close(self) -> None:
        with self._cond:
            self._open = False
            assert self._cond.wait_for(lambda: self._active == 0, timeout=30)

    def reopen(self) -> None:
        with self._cond:
            self._open = True
            self._cond.notify_all()


class Process:
    """One Studio 'process' (Studio + coordinator + runner maintenance) behind a fixed app."""

    def __init__(self, tmp_path: Path) -> None:
        self.settings = make_settings(tmp_path, coordinator=False)
        self.settings.execution = "nodes"
        self.settings.runner_offer_ttl_s = 5
        self.settings.runner_maintenance_s = 0.2
        app = create_app(self.settings, build_studio(self.settings))
        self.gate = Gate()
        app.middleware("http")(self.gate)
        self.client = TestClient(app)
        self.client.__enter__()
        self.api = Api(self.client, app.state.studio)
        self.coord: Coordinator | None = None
        self.maint: RunnerMaintenance | None = None

    def start(self) -> None:
        studio = self.api.studio
        self.coord, self.maint = Coordinator(studio), RunnerMaintenance(studio)
        self.coord.start()
        self.maint.start()

    def crash(self) -> None:
        """Closing the journal (under its lock: sqlite segfaults on a concurrent close) makes the in-flight lane
        thread die on its next read instead of finishing the task; registry and auth go once the lanes are gone."""
        assert self.coord is not None and self.maint is not None
        self.gate.close()
        studio = self.api.studio
        self.maint.stop()
        with studio.journal._lock:
            studio.journal.close()
        self.coord.stop()
        studio.registry.close_all()
        studio.auth.close()

    def restart(self, reopen: bool = True) -> None:
        self.crash()
        self.api.studio = build_studio(self.settings)
        self.client.app.state.studio = self.api.studio  # type: ignore[attr-defined]
        self.start()
        if reopen:
            self.gate.reopen()

    def shutdown(self) -> None:
        self.gate.reopen()
        if self.coord is not None and self.maint is not None:
            self.maint.stop()
            self.coord.stop()
        self.api.studio.close()
        self.client.__exit__(None, None, None)


@pytest.fixture
def proc(tmp_path: Path) -> Iterator[Process]:
    p = Process(tmp_path)
    p.start()
    yield p
    p.shutdown()


@pytest.fixture
def runners(proc: Process, tmp_path: Path) -> Iterator[Callable[..., Runner]]:
    started: list[Runner] = []

    def make(**kw: Any) -> Runner:
        r = Runner(proc.api, tmp_path, **kw).start()
        started.append(r)
        return r
    yield make
    proc.gate.reopen()
    for r in started:
        r.stop()
        assert r.errors == []


def _generating(api: Api) -> tuple[str, str, Any]:
    """Project + batch with prompts confirmed; returns (project, batch, the generate task once its first image call
    is executing on the runner)."""
    pid = setup_project(api)
    bid = create(api, pid, ["Tavern interior"], "rec-single-1")["batch"]["id"]
    api.wait_ops()
    confirm_all(api, pid, bid, "rec-confirm-1")
    wait_for(lambda: any(a["operation"] == "image.t2i" and a["state"] == "executing" for a in all_attempts(api)))
    (task,) = [t for t in api.studio.journal.tasks.list() if t.stage == "generate"]
    return pid, bid, task


def test_restart_reconciles_a_running_task_without_replacing_its_attempt(
        proc: Process, runners: Callable[..., Runner]) -> None:
    api = proc.api
    r1 = runners(name="r1", image_steps=1500)
    r2 = runners(name="r2", image_steps=1500)  # idle and eligible: it would receive a re-placed offer
    pid, bid, task = _generating(api)
    first = next(a for a in all_attempts(api) if a["operation"] == "image.t2i")
    executing_on = r1 if first["runner_id"] == r1.agent.state.get_identity("runner_id") else r2
    other = r2 if executing_on is r1 else r1
    proc.restart(reopen=False)  # runner requests wait until Studio is back
    t = api.studio.journal.tasks.get(task.id)
    assert t is not None and t.state == "reconciling" and t.control == "run"
    assert api.studio.journal.attempts.get(first["id"])["state"] == "executing"  # type: ignore[index]
    time.sleep(0.6)  # several maintenance cycles: still no requeue while the runner has not delivered
    assert api.studio.journal.tasks.get(task.id).state == "reconciling"  # type: ignore[union-attr]
    for r in (r1, r2):
        r.engines.comfy.steps_to_finish = 1  # the simulated work completes once the runner can deliver
    proc.gate.reopen()
    api.wait_ops()
    done = api.studio.journal.tasks.get(task.id)
    assert done is not None and done.state == "succeeded" and done.progress.get("reconciled_after_restart") == 1
    assert len(detail(api, pid, bid)["items"][0]["candidate_set"]["candidates"]) == 4
    rows = [a for a in all_attempts(api) if a["operation"] == "image.t2i"]
    assert len(rows) == 4 and {a["generation"] for a in rows} == {1}  # nothing was re-placed
    assert rows[0]["id"] == first["id"] and rows[0]["runner_id"] == first["runner_id"]
    assert all(a["state"] == "committed" for a in rows)
    submits = [c for c in executing_on.engines.comfy.calls if c[0] == "submit"]
    assert len(submits) + len([c for c in other.engines.comfy.calls if c[0] == "submit"]) == 4  # no duplicate work


def test_restart_with_a_pending_cancel_waits_for_the_runner(proc: Process, runners: Callable[..., Runner]) -> None:
    api = proc.api
    runner = runners(image_steps=1500)
    _, _, task = _generating(api)
    first = next(a for a in all_attempts(api) if a["operation"] == "image.t2i")
    proc.gate.close()  # the runner cannot hear about the cancel before Studio dies
    api.studio.journal.tasks.request_cancel([task.id])
    assert api.studio.journal.tasks.get(task.id).control == "cancel_requested"  # type: ignore[union-attr]
    proc.restart(reopen=False)
    time.sleep(0.6)  # the runner has not acknowledged: the cancel stays pending, it is never assumed
    t = api.studio.journal.tasks.get(task.id)
    assert t is not None and t.state == "reconciling" and t.control == "cancel_requested"
    a = api.studio.journal.attempts.get(first["id"])
    assert a is not None and a["state"] == "executing" and a["control"] == "cancel"
    proc.gate.reopen()
    wait_for(lambda: api.studio.journal.tasks.get(task.id).state == "cancelled")  # type: ignore[union-attr]
    assert api.studio.journal.attempts.get(first["id"])["state"] == "cancelled"  # type: ignore[index]
    sub = next(c[1] for c in runner.engines.comfy.calls if c[0] == "submit")
    assert ("cancel", sub) in runner.engines.comfy.calls
    assert len([x for x in all_attempts(api) if x["operation"] == "image.t2i"]) == 1  # no further calls
    assert [x["generation"] for x in all_attempts(api)] == [1] * len(all_attempts(api))
