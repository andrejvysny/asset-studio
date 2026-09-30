"""Process-level failure-injection harness: Studio, a compute runner and fake engine servers are SEPARATE OS
processes, each killable with SIGKILL independently (make test-process).

Engine mode: the runner runs with `simulated: false`, so its EngineExecutor talks real HTTP to a fake aux PROCESS
(tests/process/fake_engines.py, real lease/drain semantics) and delays are real. The only thing not real is model
verification: the runner's receipt cache is pre-seeded with *simulated* receipts for Studio's real catalog sha (the
same function the runner uses with `simulated: true`), because real receipts would need the multi-GB weights.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from assetstudio_core.canonical import sha256_json
from assetstudio_node.models import load_lock
from assetstudio_node.receipts import _RECEIPTS, model_receipts
from assetstudio_node.state import RunnerState

ROOT = Path(__file__).resolve().parents[2]
FAKE_ENGINES = ROOT / "tests" / "process" / "fake_engines.py"
HEADERS = {"x-assetstudio": "1"}
SLOT = "gpu-aux"
# Short runner timings so lease expiry / maintenance happen within seconds.
# The protocol floors are heartbeat >= 5 s and lease >= 15 s (SessionAccepted); the agent only heartbeats between steps,
# so holds inside one attempt must stay well under the lease unless the test wants it to expire.
STUDIO_TIMINGS = {"STUDIO_RUNNER_HEARTBEAT_S": "5", "STUDIO_RUNNER_LEASE_S": "15", "STUDIO_RUNNER_OFFER_TTL_S": "10",
                  "STUDIO_RUNNER_MAINTENANCE_S": "0.3"}
CONCEPT = {"categories": [{"id": "concept", "slug": "concept", "label": "Concept",
                           "defaults": {"kind": "concept_art", "naming": "concept_{name}"}}]}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(pred: Callable[[], Any], what: str, timeout: float = 45.0, every: float = 0.1) -> Any:
    """Poll `pred` until truthy (connection errors count as 'not yet'); fail with `what` on timeout."""
    end, last = time.monotonic() + timeout, None
    while time.monotonic() < end:
        try:
            last = pred()
        except (httpx.HTTPError, OSError, KeyError, IndexError, StopIteration) as e:
            last = e
        else:
            if last:
                return last
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for: {what} (last: {last!r})")


class Proc:
    """One OS process with a log FILE (never a pipe nobody drains). SIGKILL is the failure injection."""

    def __init__(self, name: str, argv: list[str], env: dict[str, str], log: Path) -> None:
        self.name, self.argv, self.env, self.log = name, argv, env, log
        self.popen: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        assert self.alive is False, f"{self.name} already running"
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open("ab") as f:
            f.write(f"\n--- start {self.name} ---\n".encode())
            self.popen = subprocess.Popen(self.argv, env=self.env, stdout=f, stderr=subprocess.STDOUT, cwd=ROOT,
                                          start_new_session=True)

    @property
    def alive(self) -> bool:
        return self.popen is not None and self.popen.poll() is None

    def kill9(self) -> None:
        assert self.popen is not None and self.alive, f"{self.name} is not running"
        os.kill(self.popen.pid, signal.SIGKILL)
        self.popen.wait(timeout=10)

    def stop(self) -> None:
        if self.popen is None or self.popen.poll() is not None:
            return
        self.popen.kill()
        self.popen.wait(timeout=10)

    def tail(self, n: int = 4000) -> str:
        return self.log.read_text(errors="replace")[-n:] if self.log.exists() else ""


class Studio(Proc):
    def __init__(self, tmp: Path) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.roots = (tmp / "projects").resolve()
        self.roots.mkdir(exist_ok=True)
        env = {**os.environ, **STUDIO_TIMINGS, "STUDIO_INSTANCE_DIR": str((tmp / "instance").resolve()),
               "STUDIO_PROJECT_ROOTS": str(self.roots), "STUDIO_PORT": str(self.port), "STUDIO_HOST": "127.0.0.1",
               "STUDIO_EXECUTION": "nodes", "STUDIO_PUBLIC_URL": self.url, "STUDIO_WEB_DIR": str(tmp / "no-web"),
               "STUDIO_MODELS_ROOT": str(tmp / "no-models")}
        super().__init__("studio", [sys.executable, "-m", "assetstudio_server.cli", "serve"], env,
                         tmp / "studio.log")

    def start(self) -> None:
        super().start()
        wait_until(lambda: self.alive and httpx.get(f"{self.url}/api/health", timeout=1).status_code == 200,
                   f"studio healthy\n{self.tail()}", timeout=60)

    def restart(self) -> None:
        if self.alive:
            self.kill9()
        self.start()

    # -- API (fresh connection per call: a killed Studio must never leave a dead keep-alive socket behind) --------
    def get(self, path: str, **kw: Any) -> Any:
        r = httpx.get(self.url + path, timeout=10, **kw)
        assert r.status_code == 200, f"GET {path}: {r.status_code} {r.text}"
        return r.json()

    def post(self, path: str, body: Any = None, ok: tuple[int, ...] = (200, 201, 202)) -> Any:
        r = httpx.post(self.url + path, json=body, headers=HEADERS, timeout=30)
        assert r.status_code in ok, f"POST {path}: {r.status_code} {r.text}"
        return r.json()

    def patch(self, path: str, body: Any) -> Any:
        r = httpx.patch(self.url + path, json=body, headers=HEADERS, timeout=30)
        assert r.status_code == 200, f"PATCH {path}: {r.status_code} {r.text}"
        return r.json()


class Engine(Proc):
    def __init__(self, tmp: Path, kind: str = "aux", delay_s: float = 0.0, drain_timeout: float = 1.0) -> None:
        self.port, self.kind = free_port(), kind
        self.url = f"http://127.0.0.1:{self.port}"
        super().__init__(f"fake-{kind}", [sys.executable, str(FAKE_ENGINES), "--port", str(self.port), "--kind", kind,
                                          "--delay-s", str(delay_s), "--drain-timeout", str(drain_timeout)],
                         {**os.environ}, tmp / f"fake-{kind}-{self.port}.log")

    def start(self) -> None:
        super().start()
        wait_until(lambda: httpx.get(f"{self.url}/health", timeout=1).status_code == 200,
                   f"{self.name} healthy\n{self.tail()}")

    def control(self, *, hold: bool | None = None, delay_s: float | None = None) -> None:
        body = {k: v for k, v in (("hold", hold), ("delay_s", delay_s)) if v is not None}
        assert httpx.post(f"{self.url}/_control", json=body, timeout=5).status_code == 200

    def log_(self) -> dict[str, Any]:
        return httpx.get(f"{self.url}/_log", timeout=5).json()

    def requests(self, path: str = "/enhance") -> list[dict[str, Any]]:
        return [r for r in self.log_()["requests"] if r["path"] == path]


class Runner(Proc):
    """`assetstudio-node run` over a persistent state dir: restart() after kill9() is a crash-restart."""

    def __init__(self, tmp: Path, studio: Studio, engine: Engine, name: str = "r1") -> None:
        self.name_, self.state_dir, self.studio, self.engine = name, tmp / f"state-{name}", studio, engine
        self.token_file, self.config = tmp / f"token-{name}", tmp / f"runner-{name}.yaml"
        self.runner_id: str | None = None
        cfg = {"studio_url": studio.url, "name": name, "state_dir": str(self.state_dir),
               "host_lock": str(tmp / f"host-{name}.lock"), "registration_token_file": str(self.token_file),
               "dispatch": "pull", "acquire_wait_s": 1, "simulated": False, "models_root": str(tmp / "models"),
               "engines": {"aux": engine.url},
               "slots": [{"slot_id": SLOT, "capability": "aux3d", "devices": ["index:0"], "engines": ["aux"]}]}
        (tmp / "models").mkdir(exist_ok=True)
        self.config.write_text(json.dumps(cfg))  # JSON is YAML
        super().__init__(f"runner-{name}", [sys.executable, "-m", "assetstudio_node.cli", "run", "--config",
                                            str(self.config)], {**os.environ}, tmp / f"runner-{name}.log")

    def register(self) -> None:
        """Operator flow: group + single-use registration token; the runner registers itself on first start."""
        gid = self.studio.post("/api/v1/runner-groups", {"name": f"g-{self.name_}", "projects": "*",
                                                          "operations": "*", "labels": [], "ephemeral": False})["id"]
        self.token_file.write_text(self.studio.post(f"/api/v1/runner-groups/{gid}/registration-tokens",
                                                    {"ttl_s": 600})["token"])
        self._seed_receipts()

    def _seed_receipts(self) -> None:
        catalog = load_lock(ROOT / "config")
        sha = sha256_json(catalog)
        receipts = model_receipts(catalog, sha, None, {"aux"}, None, simulated=True)
        state = RunnerState(self.state_dir)
        try:
            state.set_identity(f"receipts:{sha}", _RECEIPTS.dump_json(receipts).decode())
        finally:
            state.close()

    def start(self) -> None:
        super().start()
        self.runner_id = wait_until(lambda: self._identity(), f"{self.name} registered\n{self.tail()}")

    def _identity(self) -> str | None:
        runners = self.studio.get("/api/v1/runners")["runners"]
        return next((r["id"] for r in runners if r["name"] == self.name_), None)

    def detail(self) -> dict[str, Any]:
        assert self.runner_id
        return self.studio.get(f"/api/v1/runners/{self.runner_id}")

    def local_attempts(self) -> list[tuple[str, str]]:
        """(attempt_id, state) straight from the runner's sqlite state (read-only; works while it is dead)."""
        import sqlite3

        db = self.state_dir / "runner.sqlite"
        if not db.exists():
            return []
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            return [(r[0], r[1]) for r in con.execute("SELECT attempt_id, state FROM attempts")]
        finally:
            con.close()

    def spool_dirs(self) -> list[str]:
        spool = self.state_dir / "spool"
        return sorted(p.name for p in spool.iterdir() if p.is_dir()) if spool.exists() else []


@dataclass
class World:
    tmp: Path
    studio: Studio
    engine: Engine
    runner: Runner
    project: str | None = None
    _procs: list[Proc] = field(default_factory=list)

    # -- driving real work through the public API ------------------------------------------------------------------
    def start_job(self, key: str = "process-batch-1") -> tuple[str, str]:
        """Concept-art batch with one item: Studio's first stage task is an `aux.enhance` remote call."""
        pid = self.studio.post("/api/v1/projects", {"name": "Demo"})["id"]
        cfg = self.studio.get(f"/api/v1/projects/{pid}/config")["config"]
        self.studio.patch(f"/api/v1/projects/{pid}/config", {"expected_revision": 1, "config": {**cfg, **CONCEPT}})
        out = self.studio.post(f"/api/v1/projects/{pid}/batches", {
            "title": "t", "category_id": "concept", "idempotency_key": key,
            "items": [{"name": "Tavern interior", "brief": "brief for Tavern interior"}]})
        self.project = pid
        return pid, out["batch"]["id"]

    def tasks(self) -> list[dict[str, Any]]:
        return self.studio.get(f"/api/v2/tasks?project_id={self.project}")["tasks"]

    def enhance_task(self) -> dict[str, Any]:
        (task,) = [t for t in self.tasks() if t["stage"] == "enhance"]
        return task

    def remote_call(self) -> dict[str, Any]:
        """The enhance task's latest attempt as Studio noted it (an unplaced offer has no runner, so it is not in
        the runner's own attempt list)."""
        return self.enhance_task()["progress"]["remote"]["enhance"]

    def attempts(self) -> list[dict[str, Any]]:
        return self.runner.detail()["attempts"]

    def attempt(self, **want: Any) -> dict[str, Any] | None:
        return next((a for a in self.attempts() if all(a[k] == v for k, v in want.items())), None)

    def device_claims(self) -> dict[str, str]:
        return {d["uuid"]: d["claim"] for d in self.runner.detail()["devices"]}

    def diagnostics(self) -> str:
        parts = [f"--- {p.name} ---\n{p.tail(1500)}" for p in (self.studio, self.runner, self.engine)]
        return "\n".join(parts)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    """Studio + fake aux + registered runner, all started and healthy. Everything is SIGKILLed in teardown."""
    tmp = tmp_path.resolve()
    studio, engine = Studio(tmp), Engine(tmp)
    runner = Runner(tmp, studio, engine)
    procs: list[Proc] = [runner, engine, studio]
    w = World(tmp, studio, engine, runner, _procs=procs)
    try:
        studio.start()
        engine.start()
        runner.register()
        runner.start()
        wait_until(lambda: runner.detail()["session"] is not None
                   and [s["state"] for s in runner.detail()["slots"]] == ["ready"], "runner session with a ready slot\n"
                   + w.diagnostics())
        yield w
    finally:
        for p in procs:
            p.stop()


@pytest.fixture
def fake_engine_proc(tmp_path: Path) -> Iterator[Callable[..., Engine]]:
    """Factory for fake engine processes (aux | worker3d); all are SIGKILLed in teardown."""
    started: list[Engine] = []

    def make(kind: str = "aux", **kw: Any) -> Engine:
        e = Engine(tmp_path.resolve(), kind, **kw)
        started.append(e)
        e.start()
        return e

    yield make
    for e in started:
        e.stop()
